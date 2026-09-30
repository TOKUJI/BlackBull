"""WebSocketActor end-of-connection release (BLA-363).

Invariant: when ``run()`` ends — however it ends — no recipient reader task
of that connection is left without a consumer.  A bounded queue can park the
reader inside ``queue.put``, and a silent transport parks it inside a read;
neither wakes on EOF alone.  H1 and H2 share ``WebSocketActor.run``, so the
enumeration below covers both transports' actor end.
"""

import asyncio
import struct
from unittest.mock import AsyncMock

import pytest

from blackbull.event_aggregator import EventAggregator
from blackbull.server.recipient import AbstractReader, _WS_READ_INLINE
from blackbull.server.sender import AbstractWriter
from blackbull.server.websocket_actor import WebSocketActor
from blackbull.server.constants import WSCloseCode

pytestmark = pytest.mark.asyncio


def _agg(*, listener_states=None, on_disconnect=None):
    """Spec'd aggregator: predicates need return values (beartype)."""
    agg = AsyncMock(spec=EventAggregator)
    agg.has_request_completed_listeners.return_value = False
    agg.has_request_disconnected_listeners.return_value = False
    if listener_states is None:
        agg.has_websocket_message_listeners.return_value = False
    else:
        agg.has_websocket_message_listeners.side_effect = listener_states
    if on_disconnect is not None:
        agg.on_websocket_disconnected.side_effect = on_disconnect
    return agg


def _ws_conn():
    from blackbull.connection import Connection
    from blackbull.headers import Headers
    return Connection(method='GET', path='/ws', raw_path=b'/ws',
                      headers=Headers([]), type='websocket')


def _client_frame(payload: bytes, opcode: int = 0x1) -> bytes:
    mask = b'\x37\xfa\x21\x3d'
    length = len(payload)
    header = bytes([0x80 | opcode])
    header += bytes([0x80 | length]) if length < 126 else (
        bytes([0x80 | 126]) + length.to_bytes(2, 'big'))
    masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    return header + mask + masked


class _ScriptedReader(AbstractReader):
    """Serve scripted chunks; park forever once they run out.

    ``entries`` counts read entries (with a marker per entry) so a handler
    can wait for the background reader to reach a particular read.
    """

    def __init__(self, chunks):
        self._chunks = list(chunks)
        self.entries = 0
        self.entered = asyncio.Queue()

    async def read(self, n: int) -> bytes:
        self.entries += 1
        self.entered.put_nowait(self.entries)
        while not self._chunks:
            await asyncio.Event().wait()
        chunk = self._chunks.pop(0)
        if len(chunk) > n:
            self._chunks.insert(0, chunk[n:])
            chunk = chunk[:n]
        return chunk


class _FakeWriter(AbstractWriter):
    def __init__(self):
        self.written = bytearray()
        self.closed = False

    async def write(self, data: bytes) -> None:
        self.written += data

    async def close(self) -> None:
        self.closed = True


async def _settle():
    for _ in range(25):
        await asyncio.sleep(0)


def _released(actor) -> bool:
    task = actor._ws_receive._reader_task
    return task is None or task.done()


def _make_actor(chunks, app, *, depth=1, agg=None):
    return WebSocketActor(
        _ScriptedReader(chunks), _FakeWriter(), _ws_conn(), app,
        agg or _agg(), ws_queue_depth=depth)


class TestActorEndReleasesTheReader:
    async def test_handler_return_releases_a_reader_parked_on_a_full_queue(self):
        async def app(conn, receive, send):
            await receive()                       # websocket.connect
            await send({'type': 'websocket.accept', 'subprotocol': None})
            await receive()                       # one message
            return

        actor = _make_actor([_client_frame(b'a'), _client_frame(b'b'),
                             _client_frame(b'c')], app, depth=1)
        await actor.run()
        await _settle()
        assert _released(actor)

    async def test_handler_return_releases_a_reader_parked_on_a_read(self):
        async def app(conn, receive, send):
            await receive()
            await send({'type': 'websocket.accept', 'subprotocol': None})
            await receive()
            return

        actor = _make_actor([_client_frame(b'a')], app, depth=1)
        await actor.run()
        await _settle()
        assert _released(actor)

    async def test_a_deferred_reader_spawned_by_the_observer_switch_is_released(self):
        states = iter([False, False, False, True, True, True, True, True])
        agg = _agg(listener_states=lambda: next(states, True))
        holder = {}

        async def app(conn, receive, send):
            await receive()
            await send({'type': 'websocket.accept', 'subprotocol': None})
            await receive()                       # inline read
            await receive()                       # the observer is watching now
            # The watchdog tick is what starts the deferred read-ahead task.
            holder['actor']._ws_receive._on_idle_tick()
            return

        actor = _make_actor([_client_frame(b'a'), _client_frame(b'b'),
                             _client_frame(b'c')], app,
                            depth=_WS_READ_INLINE, agg=agg)
        holder['actor'] = actor
        await actor.run()
        await _settle()
        assert _released(actor)

    async def test_handler_exception_releases_and_reports(self):
        async def app(conn, receive, send):
            await receive()
            await send({'type': 'websocket.accept', 'subprotocol': None})
            raise RuntimeError('handler blew up')

        agg = _agg()
        actor = _make_actor([_client_frame(b'a')], app, depth=1, agg=agg)
        await actor.run()
        await _settle()
        assert _released(actor)
        agg.on_error.assert_awaited_once()

    async def test_handler_cancel_releases(self):
        async def app(conn, receive, send):
            await receive()
            await send({'type': 'websocket.accept', 'subprotocol': None})
            await asyncio.Event().wait()

        actor = _make_actor([], app, depth=1)
        run = asyncio.ensure_future(actor.run())
        await _settle()
        run.cancel()
        with pytest.raises(asyncio.CancelledError):
            await run
        await _settle()
        assert _released(actor)

    async def test_peer_close_releases_and_reports_the_code_once(self):
        async def app(conn, receive, send):
            await receive()
            await send({'type': 'websocket.accept', 'subprotocol': None})
            while True:
                msg = await receive()
                if msg.get('type') == 'websocket.disconnect':
                    break

        agg = _agg()
        actor = _make_actor(
            [_client_frame(struct.pack('!H', 1000), opcode=0x8)], app,
            depth=1, agg=agg)
        await actor.run()
        await _settle()
        assert _released(actor)
        agg.on_websocket_disconnected.assert_awaited_once()
        assert (agg.on_websocket_disconnected.call_args.kwargs['code'] == 1000)

    async def test_an_unresponsive_peer_close_releases_and_reports_1001(self):
        async def app(conn, receive, send):
            await receive()
            await send({'type': 'websocket.accept', 'subprotocol': None})
            await receive()                       # ends when the timeout closes
            return

        agg = _agg()
        actor = _make_actor([], app, depth=1, agg=agg)
        run = asyncio.ensure_future(actor.run())
        await _settle()
        await actor._ws_receive._end_for_unresponsive_peer()   # idle timeout
        await run
        await _settle()
        assert _released(actor)
        agg.on_websocket_disconnected.assert_awaited_once()
        assert (agg.on_websocket_disconnected.call_args.kwargs['code']
                == WSCloseCode.GOING_AWAY)

    async def test_post_handshake_failure_releases(self):
        async def app(conn, receive, send):
            await receive()
            await send({'type': 'websocket.accept', 'subprotocol': None,
                        'headers': [[b'sec-websocket-accept', b'evil']]})

        agg = _agg()
        actor = _make_actor([_client_frame(b'a')], app, depth=1, agg=agg)
        await actor.run()
        await _settle()
        assert _released(actor)
        agg.on_error.assert_awaited_once()

    async def test_a_raising_disconnect_listener_still_releases_reader_and_writer(self):
        async def app(conn, receive, send):
            await receive()
            await send({'type': 'websocket.accept', 'subprotocol': None})
            return

        agg = _agg(on_disconnect=RuntimeError('listener blew up'))
        actor = _make_actor([_client_frame(b'a')], app, depth=1, agg=agg)
        with pytest.raises(RuntimeError, match='listener blew up'):
            await actor.run()
        await _settle()
        assert _released(actor)
        assert actor._writer.closed

    async def test_shutdown_twice_is_idempotent(self):
        async def app(conn, receive, send):
            await receive()
            await send({'type': 'websocket.accept', 'subprotocol': None})
            await receive()
            return

        actor = _make_actor([_client_frame(b'a')], app, depth=1)
        await actor.run()
        await actor._ws_receive.shutdown()        # second release must be a no-op
        assert _released(actor)

class _StubbornReader(_ScriptedReader):
    """A reader that suppresses cancellation, as the channel contract allows."""

    def __init__(self, chunks):
        super().__init__(chunks)
        self.suppressing = True

    async def read(self, n: int) -> bytes:
        while self.suppressing:
            try:
                return await super().read(n)
            except asyncio.CancelledError:
                continue
        return b''


class TestTheReleaseIsBounded:
    """No step of the release may wait forever on app- or reader-side code."""

    async def test_a_hanging_disconnect_listener_does_not_stall_the_release(
            self, monkeypatch):
        from blackbull.server import websocket_actor
        monkeypatch.setattr(websocket_actor,
                            '_DISCONNECT_HOOK_TIMEOUT', 0.05)

        async def hang(conn, code=None):
            await asyncio.Event().wait()

        async def app(conn, receive, send):
            pass

        writer = _FakeWriter()
        actor = WebSocketActor(
            _ScriptedReader([_client_frame(b'hi')]), writer, _ws_conn(), app,
            _agg(on_disconnect=hang))
        await asyncio.wait_for(actor.run(), 1.0)
        assert writer.closed, 'the transport must close past a hanging listener'

    async def test_a_reader_that_suppresses_cancellation_does_not_stall_shutdown(
            self, monkeypatch):
        from blackbull.server import recipient
        monkeypatch.setattr(recipient, '_READ_LOOP_JOIN_TIMEOUT', 0.05)

        async def app(conn, receive, send):
            await receive()
            await receive()

        reader = _StubbornReader([_client_frame(b'hi')])
        writer = _FakeWriter()
        actor = WebSocketActor(reader, writer, _ws_conn(), app, _agg(),
                               ws_queue_depth=1)
        await asyncio.wait_for(actor.run(), 1.0)
        assert writer.closed, 'the transport must close past a suppressing reader'
        reader.suppressing = False

