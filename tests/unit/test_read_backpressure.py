"""Backpressure must not starve a reader that is waiting for a large frame.

The read buffer pauses the transport once unconsumed bytes reach the high-water
mark, so a fast peer cannot grow it without bound while a slow handler falls
behind.  That is the right rule for a handler that is *behind*.  It is exactly
wrong for one that is **blocked waiting for more**: a reader parked in
``readexactly(n)`` with ``n`` above the mark can never be satisfied, because the
bytes it is waiting for are the ones the pause is refusing to read.

The connection then hangs until the peer gives up — no error, no log, no
response.  Two real shapes reach it:

* a WebSocket frame above the mark (Autobahn's 9.x large-message cases are
  built on exactly this, up to 8 MiB);
* a single ``Transfer-Encoding: chunked`` chunk above the mark, whose size the
  *peer* chooses.

A ``Content-Length`` body is not affected only because ``BB_BODY_CHUNK_SIZE``
(64 KiB) keeps each ``readexactly`` under the mark — luck, not design, and it
is why the deadlock survived a green suite.

The rule: **parking to wait releases the pause.**  A reader that is waiting is
starved, not behind, so the condition backpressure exists to prevent is not the
one in play.  This is what ``asyncio.StreamReader._wait_for_data`` does, for
the same reason.
"""
import asyncio
import socket

import pytest

from blackbull import BlackBull
from blackbull.client.websocket import WebSocketClient
from blackbull.server.connection_protocol import _HIGH_WATER, ConnectionProtocol
from blackbull.testing.native import NativeTestServer
from blackbull.utils import Scheme

pytestmark = pytest.mark.asyncio

#: Comfortably past the mark, small enough to stay a fast test.
_OVER = _HIGH_WATER * 8


class _FakeTransport:
    def __init__(self) -> None:
        self.reading = True
        self.pauses = 0
        self.resumes = 0

    def pause_reading(self):
        self.reading = False
        self.pauses += 1

    def resume_reading(self):
        self.reading = True
        self.resumes += 1

    def write(self, data): pass
    def close(self): pass
    def is_closing(self): return False
    def get_extra_info(self, name, default=None): return default


def _fill(proto: ConnectionProtocol, n: int) -> None:
    """Deliver *n* bytes, honouring the pause the way a transport would."""
    remaining = n
    while remaining and proto.transport.reading:
        view = proto.get_buffer(-1)
        take = min(len(view), remaining)
        view[:take] = b'z' * take
        proto.buffer_updated(take)
        remaining -= take


# ---------------------------------------------------------------------------
# Unit: the pause and its release
# ---------------------------------------------------------------------------

async def test_transport_is_paused_at_the_high_water_mark():
    """The backpressure itself still works — this is what must not regress
    while fixing the starvation below."""
    proto = ConnectionProtocol()
    proto.connection_made(_FakeTransport())
    _fill(proto, _HIGH_WATER * 2)
    assert proto.transport.pauses == 1
    assert not proto.transport.reading


async def test_waiting_for_data_releases_the_pause():
    """A parked reader is starved, not behind.

    Driven through the reader: deciding to wait and releasing the pause that
    goes with it are one receive decision, so they live together on
    ``BufferReader`` while the protocol keeps only the rendezvous.
    """
    proto = ConnectionProtocol()
    proto.connection_made(_FakeTransport())
    _fill(proto, _HIGH_WATER * 2)
    assert not proto.transport.reading

    waiter = asyncio.create_task(proto.reader.wait_for_data())
    await asyncio.sleep(0)
    assert proto.transport.reading, (
        'the reader parked without resuming the transport — the bytes it '
        'is waiting for can never arrive')

    _fill(proto, 64)
    await asyncio.wait_for(waiter, timeout=1)


async def test_a_large_read_does_not_churn_pause_resume():
    """The pause must not be re-armed on every arrival while a reader is parked.

    ``wait_for_data`` would immediately release it again, so arming it costs a
    ``pause_reading``/``resume_reading`` pair — two ``epoll_ctl`` calls — per
    ``recv`` for the whole of a large read.  Correctness is unaffected either
    way, which is exactly why this needs its own assertion: without one the
    churn can come back silently.
    """
    proto = ConnectionProtocol()
    proto.connection_made(_FakeTransport())
    want = _HIGH_WATER * 8

    task = asyncio.create_task(proto.reader.readexactly(want))
    await asyncio.sleep(0)                    # let the reader park

    arrivals = 0
    while arrivals * 8192 < want:
        view = proto.get_buffer(-1)
        take = min(len(view), 8192)
        view[:take] = b'z' * take
        proto.buffer_updated(take)
        arrivals += 1
        await asyncio.sleep(0)

    assert len(await asyncio.wait_for(task, timeout=5)) == want
    assert arrivals >= 64, 'harness must deliver many arrivals to be meaningful'
    # A handful is fine (the waiter briefly clears as it is woken); one per
    # arrival is the regression.
    assert proto.transport.pauses <= 4, (
        f'{proto.transport.pauses} pauses over {arrivals} arrivals — the '
        f'pause is being re-armed while a reader is parked')


async def test_readexactly_past_the_mark_completes():
    """The unit-level form of both end-to-end hangs below."""
    proto = ConnectionProtocol()
    proto.connection_made(_FakeTransport())

    async def _feed():
        for _ in range(64):
            await asyncio.sleep(0)
            _fill(proto, _OVER)

    feeder = asyncio.create_task(_feed())
    got = await asyncio.wait_for(proto.reader.readexactly(_OVER), timeout=5)
    feeder.cancel()
    assert len(got) == _OVER


# ---------------------------------------------------------------------------
# End-to-end: the two shapes that actually reach it over a socket
# ---------------------------------------------------------------------------

async def test_websocket_frame_past_the_mark_round_trips():
    app = BlackBull()

    @app.route(path='/ws', scheme=Scheme.websocket)
    async def _ws(conn, receive, send):
        await receive()
        await send({'type': 'websocket.accept'})
        event = await receive()
        await send({'type': 'websocket.send', 'bytes': event['bytes']})

    async with NativeTestServer(app) as srv:
        async with WebSocketClient('127.0.0.1', srv.port) as client:
            ws = await client.connect('/ws')
            await ws.send_bytes(b'z' * _OVER)
            event = await asyncio.wait_for(ws.receive(), timeout=15)

    assert len(event.get('bytes') or b'') == _OVER, (
        'a WebSocket frame larger than the high-water mark never came back — '
        'the read paused waiting for bytes it had refused to read')


async def test_single_chunked_chunk_past_the_mark_is_read():
    """The chunk size is the *peer's* choice, so no internal constant bounds
    it the way BB_BODY_CHUNK_SIZE bounds a Content-Length read."""
    app = BlackBull()

    @app.route(path='/echo', methods=['POST'])
    async def _echo(conn):
        return str(len(await conn.body()))

    def _exchange(port: int) -> bytes:
        body = b'z' * _OVER
        # ``Connection: close`` so the read-to-EOF above terminates on the
        # response rather than on the keep-alive idle timeout.
        raw = (b'POST /echo HTTP/1.1\r\nHost: x\r\nConnection: close\r\n'
               b'Transfer-Encoding: chunked\r\n\r\n'
               + format(_OVER, 'x').encode() + b'\r\n' + body
               + b'\r\n0\r\n\r\n')
        s = socket.create_connection(('127.0.0.1', port), timeout=15)
        try:
            s.sendall(raw)
            # Read to EOF, not to the end of the head: the body assertion below
            # would otherwise depend on the send path happening to put head and
            # body in one write, and would report a segmented response as "the
            # connection stalled" — the one thing this test must not misreport.
            out = b''
            while True:
                chunk = s.recv(65536)
                if not chunk:
                    break
                out += chunk
            return out
        except socket.timeout:
            return b'<timed out>'
        finally:
            s.close()

    async with NativeTestServer(app) as srv:
        response = await asyncio.to_thread(_exchange, srv.port)

    assert response.startswith(b'HTTP/1.1 200'), (
        f'a single chunk larger than the high-water mark stalled the '
        f'connection; got {response[:60]!r}')
    assert str(_OVER).encode() in response
