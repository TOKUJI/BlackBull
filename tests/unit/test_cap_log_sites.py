"""Each cap in the inventory has a unit test
asserting it logs at the rejection site.

If a future PR adds a new cap (``BB_*`` env var that rejects traffic)
without wiring a ``log_cap_hit(...)`` call at the rejection site, this
file is where the new test belongs.  Until it does, the inventory
contract is not satisfied — see
``BLA-116`` [private] for the design.

Each test follows the same shape:

1. Caplog captures records on ``blackbull.caps`` at WARNING.
2. Trigger the rejection path with the minimum setup needed.
3. Assert at least one ``cap == <cap-name>`` record was emitted.

Functional behaviour of the rejection itself (returning 408, sending
RST_STREAM, dropping the frame, etc.) is covered by the existing test
suite — this file just gates that the cap-hit *log* fires.

**Test quality tiers**: tests that drive the actual rejection site
(prefixed ``test_<cap>_logs``) are preferred over tests that call
``log_cap_hit`` directly and check the signature (prefixed
``test_<cap>_logs_via_signature``).  The signature form is used only
where driving the site requires multi-connection orchestration or
full H/2 connection setup that is impractical at the unit level.
"""
from __future__ import annotations

import ast
import asyncio
import logging

import pytest

from blackbull.server.http1_actor import HTTP1Actor
from blackbull.server.recipient import AbstractReader
from blackbull.server.sender import AbstractWriter


def _conn(headers, path: str = '/'):
    """The native ``Connection`` the actor binds a recipient to.

    ``HTTP1Recipient`` frames from ``conn.headers``; it has no scope-dict
    shape, so a test that drives it directly builds the same object the
    parser would.
    """
    from blackbull.connection import Connection
    from blackbull.headers import Headers
    return Connection(method='POST', path=path, raw_path=path.encode(),
                      headers=headers if isinstance(headers, Headers)
                      else Headers(headers), type='http')


# ----------------------------------------------------------------------
# Common fixtures
# ----------------------------------------------------------------------

@pytest.fixture
def caps_caplog(caplog):
    caplog.set_level(logging.WARNING, logger='blackbull.caps')
    return caplog


def _records_for(caplog, cap_name: str):
    return [
        r for r in caplog.records
        if r.name == 'blackbull.caps' and getattr(r, 'cap', None) == cap_name
    ]


# ----------------------------------------------------------------------
# HTTP/1.1 functional-test helpers (drive the rejection sites directly)
# ----------------------------------------------------------------------

class _FakeReader(AbstractReader):
    """Reader that yields pre-buffered bytes line-by-line via readuntil.

    Used to drive ``HTTP1Actor._read_headers`` with oversized input."""
    def __init__(self, data: bytes):
        self._buf = bytearray(data)

    async def read(self, n: int = -1) -> bytes:
        if n < 0:
            chunk, self._buf = bytes(self._buf), bytearray()
            return chunk
        chunk = bytes(self._buf[:n])
        del self._buf[:n]
        return chunk

    async def readuntil(self, sep: bytes) -> bytes:
        idx = self._buf.find(sep)
        if idx == -1:
            chunk, self._buf = bytes(self._buf), bytearray()
            return chunk + sep
        chunk = bytes(self._buf[:idx + len(sep)])
        del self._buf[:idx + len(sep)]
        return chunk

    async def readexactly(self, n: int) -> bytes:
        from blackbull.server.recipient import IncompleteReadError
        if len(self._buf) < n:
            raise IncompleteReadError(bytes(self._buf))
        chunk = bytes(self._buf[:n])
        del self._buf[:n]
        return chunk


class _FakeWriter(AbstractWriter):
    """Writer that records everything and drains instantly."""
    def __init__(self):
        self.written = bytearray()
        self.closed = False

    async def write(self, data: bytes) -> None:
        self.written += data

    async def writelines(self, parts) -> None:
        for p in parts:
            self.written += p

    async def close(self) -> None:
        self.closed = True


async def _noop_app(conn, receive, send):
    pass


def _make_actor(raw: bytes, app=None, *,
                peername=('127.0.0.1', 54321),
                sockname=('0.0.0.0', 8000),
                ssl=False,
                **kwargs):
    """Create an HTTP1Actor pre-loaded with *raw* as the header block.

    Splits at the first ``\\r\\n`` to separate the request line from
    the rest, matching the pattern in ``tests/conformance/http1/``.
    Extra *kwargs* are passed to the ``HTTP1Actor`` constructor.
    """
    if app is None:
        app = _noop_app
    writer = _FakeWriter()
    first_line, rest = raw.split(b'\r\n', 1)
    reader = _FakeReader(rest)
    actor = HTTP1Actor(
        reader, writer, app, None,
        request=first_line + b'\r\n',
        peername=peername, sockname=sockname, ssl=ssl,
        **kwargs,
    )
    return actor, writer


# ----------------------------------------------------------------------
# ws_max_frame_payload (recipient.py)
# ----------------------------------------------------------------------

@pytest.mark.asyncio
async def test_ws_max_frame_payload_logs(caps_caplog):
    from blackbull.server.recipient import WebSocketRecipient, AbstractReader
    from blackbull.server.sender import AbstractWriter

    # Forge a fake reader that produces one masked frame whose declared
    # length blows past the cap.  read_frame_header reads 2 bytes;
    # length=126 forces a 2-byte extended length read.
    class _Reader(AbstractReader):
        def __init__(self, data: bytes):
            self._buf = bytearray(data)

        async def read(self, n: int) -> bytes:
            raise NotImplementedError

        async def readuntil(self, sep: bytes) -> bytes:
            raise NotImplementedError

        async def readexactly(self, n: int) -> bytes:
            if len(self._buf) < n:
                raise asyncio.IncompleteReadError(bytes(self._buf), n)
            out = bytes(self._buf[:n])
            del self._buf[:n]
            return out

    class _Writer(AbstractWriter):
        async def write(self, data: bytes) -> None:
            pass

        async def writelines(self, parts) -> None:
            pass

        async def close(self) -> None:
            pass

    # FIN=1, opcode=2 (binary), masked=1, length=126 -> extended 2-byte
    # length follows.  Set extended length to 0xFFFF — well over our test cap.
    frame = bytes([0x82, 0xFE]) + (0xFFFF).to_bytes(2, 'big') + b'\x00\x00\x00\x00'
    reader = _Reader(frame)
    writer = _Writer()

    recipient = WebSocketRecipient(
        reader=reader, writer=writer,
        conn=_conn([], '/ws'),
        max_frame_payload=1024,
    )
    recipient._event_queue = asyncio.Queue()    # _read_loop asserts non-None

    # _read_loop handles ProtocolError internally (sends CLOSE 1009 and
    # exits cleanly); the cap-hit log fires before that handling.
    await recipient._read_loop()

    assert len(_records_for(caps_caplog, 'ws_max_frame_payload')) >= 1


@pytest.mark.asyncio
async def test_ws_max_frame_payload_no_log_under_cap(caps_caplog):
    """A frame within the payload cap must NOT trigger a cap-hit log."""
    from blackbull.server.recipient import WebSocketRecipient, AbstractReader as _AR

    class _Reader(_AR):
        def __init__(self, data: bytes):
            self._buf = bytearray(data)
        async def read(self, n: int) -> bytes:
            raise NotImplementedError
        async def readuntil(self, sep: bytes) -> bytes:
            raise NotImplementedError
        async def readexactly(self, n: int) -> bytes:
            if len(self._buf) < n:
                raise asyncio.IncompleteReadError(bytes(self._buf), n)
            out = bytes(self._buf[:n])
            del self._buf[:n]
            return out

    class _Writer(AbstractWriter):
        async def write(self, data: bytes) -> None: pass
        async def writelines(self, parts) -> None: pass
        async def close(self) -> None: pass

    # Small frame: FIN=1, opcode=2 (binary), masked=1, len=5, mask + 5 bytes payload
    frame = bytes([0x82, 0x85]) + b'\x00\x00\x00\x00' + b'hello'
    reader = _Reader(frame)
    writer = _Writer()

    recipient = WebSocketRecipient(
        reader=reader, writer=writer,
        conn=_conn([], '/ws'),
        max_frame_payload=1024,
    )
    recipient._event_queue = asyncio.Queue()
    await recipient._read_loop()

    assert _records_for(caps_caplog, 'ws_max_frame_payload') == []


# ----------------------------------------------------------------------
# body_timeout (recipient.HTTP1Recipient)
# ----------------------------------------------------------------------

@pytest.mark.asyncio
async def test_body_timeout_logs(caps_caplog):
    from blackbull.server.recipient import HTTP1Recipient, AbstractReader

    class _SlowReader(AbstractReader):
        async def readexactly(self, n: int) -> bytes:
            raise asyncio.TimeoutError()

        async def read(self, n: int) -> bytes:
            raise asyncio.TimeoutError()

        async def readuntil(self, sep: bytes) -> bytes:
            raise asyncio.TimeoutError()

    conn = _conn([(b'content-length', b'1024')], '/upload')
    recipient = HTTP1Recipient(reader=_SlowReader(), conn=conn,
                               body_timeout=0.0)
    recipient._content_length = 1024  # bypass the parser

    event = await recipient()
    # HTTP_DISCONNECT is the user-visible behaviour; the cap-hit log
    # fires alongside it.
    assert event.get('type') in (b'http.disconnect', 'http.disconnect')
    assert len(_records_for(caps_caplog, 'body_timeout')) == 1


@pytest.mark.asyncio
async def test_body_timeout_no_log_when_data_arrives(caps_caplog):
    """When body data arrives without timeout, no cap-hit log fires."""
    from blackbull.server.recipient import HTTP1Recipient, AbstractReader as _AR

    class _FastReader(_AR):
        async def readexactly(self, n: int) -> bytes:
            return b'x' * n
        async def read(self, n: int) -> bytes:
            return b'x' * min(n, 1024)
        async def readuntil(self, sep: bytes) -> bytes:
            raise asyncio.IncompleteReadError(b'', 0)

    # Simple content-length request — one readexactly call, then http.disconnect.
    conn = _conn([(b'content-length', b'5')], '/upload')
    recipient = HTTP1Recipient(reader=_FastReader(), conn=conn,
                               body_timeout=30.0)
    recipient._content_length = 5  # bypass parser

    event = await recipient()
    # First call returns the body bytes via http.request
    assert event.get('type') in (b'http.request', 'http.request')
    assert event.get('body') == b'xxxxx'
    assert _records_for(caps_caplog, 'body_timeout') == []


# ----------------------------------------------------------------------
# write_timeout (sender.AsyncioWriter)
# ----------------------------------------------------------------------

@pytest.mark.asyncio
async def test_write_timeout_logs(caps_caplog):
    from blackbull.server.sender import AsyncioWriter

    class _BlockingDrainWriter:
        def write(self, data: bytes) -> None:
            pass

        async def drain(self) -> None:
            await asyncio.Event().wait()   # never resolves

        def close(self) -> None:
            pass

    writer = AsyncioWriter(_BlockingDrainWriter(), write_timeout=0.01)
    with pytest.raises(ConnectionResetError):
        await writer.write(b'payload')

    assert len(_records_for(caps_caplog, 'write_timeout')) == 1


@pytest.mark.asyncio
async def test_write_timeout_no_log_when_drain_succeeds(caps_caplog):
    """When drain completes promptly, no cap-hit log fires."""
    from blackbull.server.sender import AsyncioWriter

    class _FastDrainWriter:
        def write(self, data: bytes) -> None:
            pass
        async def drain(self) -> None:
            return None  # resolves immediately
        def close(self) -> None:
            pass

    writer = AsyncioWriter(_FastDrainWriter(), write_timeout=30.0)
    await writer.write(b'payload')  # must NOT raise
    assert _records_for(caps_caplog, 'write_timeout') == []


# ----------------------------------------------------------------------
# compression_max_inflight (middleware.compression.Compression)
# ----------------------------------------------------------------------

@pytest.mark.asyncio
async def test_compression_max_inflight_logs(caps_caplog):
    from blackbull.middleware.compression import Compression

    mw = Compression(executor_threshold=1, executor_max_inflight=1)
    mw._executor_inflight = 1  # cap already saturated

    sent = []

    async def send(event):
        sent.append(event)

    async def receive():
        return {'type': 'http.request', 'body': b'', 'more_body': False}

    body = b'x' * 256

    async def app(conn, receive, send):
        await send({'type': 'http.response.start', 'status': 200,
                    'headers': [(b'content-type', b'text/plain')]})
        await send({'type': 'http.response.body', 'body': body, 'more_body': False})

    async def call_next(conn, receive, send):
        await app(conn, receive, send)

    from blackbull.connection import Connection
    conn = Connection.from_scope({
        'type': 'http', 'method': 'GET', 'path': '/',
        'headers': [(b'accept-encoding', b'gzip')],
    })
    await mw(conn, receive, send, call_next)

    assert len(_records_for(caps_caplog, 'compression_max_inflight')) == 1


@pytest.mark.asyncio
async def test_compression_max_inflight_no_log_under_cap(caps_caplog):
    """When inflight is below the cap, no cap-hit log fires."""
    from blackbull.middleware.compression import Compression

    mw = Compression(executor_threshold=1, executor_max_inflight=10)
    mw._executor_inflight = 0  # plenty of headroom

    sent = []

    async def send(event):
        sent.append(event)

    async def receive():
        return {'type': 'http.request', 'body': b'', 'more_body': False}

    body = b'x' * 256

    async def app(conn, receive, send):
        await send({'type': 'http.response.start', 'status': 200,
                    'headers': [(b'content-type', b'text/plain')]})
        await send({'type': 'http.response.body', 'body': body, 'more_body': False})

    async def call_next(conn, receive, send):
        await app(conn, receive, send)

    conn = {
        'type': 'http', 'method': 'GET', 'path': '/',
        'headers': [(b'accept-encoding', b'gzip')],
    }
    await mw(conn, receive, send, call_next)

    assert _records_for(caps_caplog, 'compression_max_inflight') == []


# ----------------------------------------------------------------------
# stream_queue_depth (recipient.HTTP2Recipient drops)
# ----------------------------------------------------------------------

@pytest.mark.asyncio
async def test_stream_queue_depth_logs(caps_caplog):
    from blackbull.server.recipient import HTTP2Recipient

    recipient = HTTP2Recipient()
    recipient._queue_depth = 1
    # Pre-fill the queue so the next put_nowait raises QueueFull.
    q = recipient._ensure_queue()
    q.put_nowait(((b'first', False), 0))

    dropped = recipient.put_end_of_stream()
    assert dropped is False  # item was dropped
    assert len(_records_for(caps_caplog, 'stream_queue_depth')) >= 1


@pytest.mark.asyncio
async def test_stream_queue_depth_no_log_under_cap(caps_caplog):
    """When the H/2 stream queue has room, no cap-hit log fires."""
    from blackbull.server.recipient import HTTP2Recipient

    recipient = HTTP2Recipient()
    recipient._queue_depth = 10
    # Queue has room — put_nowait must succeed.
    ok = recipient.put_end_of_stream()
    assert ok is True
    assert _records_for(caps_caplog, 'stream_queue_depth') == []


# ----------------------------------------------------------------------
# header_max_line — oversized single header line (HTTP/1.1 _parse)
# ----------------------------------------------------------------------

@pytest.mark.asyncio
async def test_header_max_line_logs(caps_caplog):
    """A header line > 8 KiB triggers HeaderTooLargeError in _parse();
    run() catches it and emits the cap-hit log."""
    # Build a request where one header line is 9 KiB (over the 8 KiB default).
    big_value = b'A' * (9 * 1024)
    raw = (
        b'GET / HTTP/1.1\r\n'
        b'Host: localhost\r\n'
        b'X-Big: ' + big_value + b'\r\n'
        b'\r\n'
    )
    actor, writer = _make_actor(raw)
    # run() will call _parse() which raises HeaderTooLargeError →
    # log_cap_hit('header_max_line', ...) → send 431 → return.
    await actor.run()

    records = _records_for(caps_caplog, 'header_max_line')
    assert len(records) >= 1
    assert records[0].levelno == logging.WARNING
    assert records[0].protocol == 'http1'


@pytest.mark.asyncio
async def test_header_max_line_no_log_when_under_cap(caps_caplog):
    """A normal-sized header line must NOT trigger a cap-hit log."""
    raw = (
        b'GET / HTTP/1.1\r\n'
        b'Host: localhost\r\n'
        b'X-Normal: ' + b'a' * 200 + b'\r\n'
        b'\r\n'
    )
    actor, _ = _make_actor(raw)
    await actor.run()
    assert _records_for(caps_caplog, 'header_max_line') == []


# ----------------------------------------------------------------------
# header_max_total — header block exceeds total cap (HTTP/1.1 _read_headers)
# ----------------------------------------------------------------------

@pytest.mark.asyncio
async def test_header_max_total_logs(caps_caplog, monkeypatch):
    """Many small header lines whose sum exceeds the total cap trigger
    HeaderTooLargeError in _read_headers(); run() catches it and logs."""
    from blackbull.env import reset_settings_cache
    monkeypatch.setenv('BB_HEADER_MAX_TOTAL', '2048')
    reset_settings_cache()

    # ~100 small headers × 80 bytes each ≈ 8 KiB > 2 KiB cap.
    lines = [b'GET / HTTP/1.1', b'Host: localhost']
    for i in range(100):
        lines.append(b'X-Filler-' + str(i).encode() + b': ' + b'a' * 60)
    lines.extend([b'', b''])
    raw = b'\r\n'.join(lines)

    actor, writer = _make_actor(raw)
    await actor.run()

    records = _records_for(caps_caplog, 'header_max_total')
    assert len(records) >= 1
    assert records[0].levelno == logging.WARNING
    assert records[0].protocol == 'http1'


@pytest.mark.asyncio
async def test_header_max_total_no_log_when_under_cap(caps_caplog):
    """A header block under the total cap must NOT trigger a cap-hit log."""
    raw = (
        b'GET / HTTP/1.1\r\n'
        b'Host: localhost\r\n'
        b'X-Foo: bar\r\n'
        b'\r\n'
    )
    actor, _ = _make_actor(raw)
    await actor.run()
    assert _records_for(caps_caplog, 'header_max_total') == []


# ----------------------------------------------------------------------
# header_timeout — slow header delivery (HTTP/1.1 slowloris defence)
# ----------------------------------------------------------------------

@pytest.mark.asyncio
async def test_header_timeout_logs(caps_caplog, monkeypatch):
    """A reader that never delivers the header terminator triggers the
    header_timeout cap; run() emits the cap-hit log."""
    from blackbull.env import reset_settings_cache
    monkeypatch.setenv('BB_HEADER_TIMEOUT', '0.01')
    reset_settings_cache()

    class _SlowReader(AbstractReader):
        """A peer that connected, sent a request line, and then went quiet.

        Every access method blocks, because a reader may look for the head
        line by line via ``readuntil`` or by scanning what it has via
        ``read``.  A fake that blocks in one and returns EOF (``b''``) in the
        other is not a slow peer at all under the second: it is a peer that
        hung up, which draws a 400 instead of the 408 this test is about.  No
        socket behaves that way, so the shape is scripted once, for both.
        """
        async def read(self, n: int = -1) -> bytes:
            await asyncio.sleep(10.0)
            return b''
        async def readuntil(self, sep: bytes) -> bytes:
            await asyncio.sleep(10.0)
            return b''
        async def readexactly(self, n: int) -> bytes:
            await asyncio.sleep(10.0)
            return b''

    writer = _FakeWriter()
    # Pass only the request line; _read_headers will try to read more
    # from the slow reader and time out.
    actor = HTTP1Actor(
        _SlowReader(), writer, _noop_app, None,
        request=b'GET / HTTP/1.1\r\n',
        peername=('127.0.0.1', 54321),
        sockname=('0.0.0.0', 8000),
        ssl=False,
    )
    await actor.run()

    records = _records_for(caps_caplog, 'header_timeout')
    assert len(records) >= 1
    assert records[0].levelno == logging.WARNING
    assert records[0].protocol == 'http1'


# ----------------------------------------------------------------------
# request_timeout — per-request total timeout (HTTP/1.1 path)
# ----------------------------------------------------------------------

@pytest.mark.asyncio
async def test_request_timeout_logs(caps_caplog, monkeypatch):
    """A handler that never responds triggers the request_timeout cap;
    _dispatch_request emits the cap-hit log."""
    from blackbull.env import reset_settings_cache
    monkeypatch.setenv('BB_REQUEST_TIMEOUT', '0.01')
    reset_settings_cache()

    async def _slow_handler(conn, receive, send):
        # Never sends a response — timeout fires first.
        await asyncio.Event().wait()

    raw = b'GET / HTTP/1.1\r\nHost: localhost\r\n\r\n'
    actor, writer = _make_actor(raw, app=_slow_handler)
    await actor.run()

    records = _records_for(caps_caplog, 'request_timeout')
    assert len(records) >= 1
    assert records[0].levelno == logging.WARNING


# ----------------------------------------------------------------------
# h2_max_concurrent_streams — H/2 stream-open guard (functional)
# ----------------------------------------------------------------------

@pytest.mark.asyncio
async def test_h2_max_concurrent_streams_logs(caps_caplog):
    """When active streams reach max_concurrent_streams, _on_headers_frame
    logs the cap-hit and sends RST_STREAM REFUSED_STREAM.

    The guard is the very first check in _on_headers_frame, so we can
    drive it with minimal mocks — the frame/tg/send args are only
    accessed after the cap check returns early."""
    from unittest.mock import MagicMock, AsyncMock
    from blackbull.server.sender import AsyncioWriter
    from blackbull.server.http2_actor import HTTP2Actor
    from blackbull.protocol.stream import Stream

    writer = MagicMock()
    writer.drain = AsyncMock()
    actor = HTTP2Actor(None, AsyncioWriter(writer), _noop_app, aggregator=None)
    actor.send_frame = AsyncMock()
    actor.max_concurrent_streams = 5
    actor._active_stream_count = 5  # at cap → next stream refused

    # spec=Stream / spec=TaskGroup makes isinstance() return True so the
    # mocks satisfy beartype's signature checks on _on_headers_frame.
    stream = MagicMock(spec=Stream)
    stream.stream_id = 1
    frame = MagicMock()
    tg = MagicMock(spec=asyncio.TaskGroup)

    result = await actor._on_headers_frame(frame, stream, AsyncMock(), tg)

    assert result is True  # refused — caller must not dispatch
    records = _records_for(caps_caplog, 'h2_max_concurrent_streams')
    assert len(records) >= 1
    assert records[0].levelno == logging.WARNING
    assert records[0].protocol == 'http2'


# ----------------------------------------------------------------------
# h2_ws_max_streams_per_connection — RFC 8441 WS guard (functional)
# ----------------------------------------------------------------------

@pytest.mark.asyncio
async def test_h2_ws_max_streams_per_connection_logs(caps_caplog, monkeypatch):
    """When WS stream count reaches the per-connection cap, _handle_h2_websocket
    logs the cap-hit and sends RST_STREAM REFUSED_STREAM."""
    from unittest.mock import MagicMock, AsyncMock
    from blackbull.connection import Connection
    from blackbull.headers import Headers
    from blackbull.server.sender import AsyncioWriter
    from blackbull.server.http2_actor import HTTP2Actor
    from blackbull.env import reset_settings_cache
    from blackbull.protocol.stream import Stream

    monkeypatch.setenv('BB_H2_WS_MAX_STREAMS_PER_CONNECTION', '3')
    monkeypatch.setenv('BB_H2_ENABLE_WEBSOCKET', '1')
    reset_settings_cache()

    writer = MagicMock()
    writer.drain = AsyncMock()
    actor = HTTP2Actor(None, AsyncioWriter(writer), _noop_app, aggregator=None)
    actor.send_frame = AsyncMock()
    actor._ws_over_h2_enabled = True
    actor._ws_stream_count = 3  # at cap → next WS stream refused

    stream = MagicMock(spec=Stream)
    # ``stream.conn`` is the native Connection on every lane, WebSocket
    # included — the cap log reads ``conn.path`` straight off it.
    stream.conn = Connection(type='websocket', method='GET', path='/ws',
                             raw_path=b'/ws', http_version='2',
                             headers=Headers([]))
    stream.stream_id = 5
    tg = MagicMock(spec=asyncio.TaskGroup)
    log_record = MagicMock()

    await actor._handle_h2_websocket(stream, tg, log_record)

    records = _records_for(caps_caplog, 'h2_ws_max_streams_per_connection')
    assert len(records) >= 1
    assert records[0].levelno == logging.WARNING
    assert records[0].protocol == 'h2-ws'


# ----------------------------------------------------------------------
# client_min_body_rate — the async client's response-body rate floor
# ----------------------------------------------------------------------

class _DrippingPeer(AbstractReader):
    """A response body arriving at 10 B/s, on a clock the test moves."""

    def __init__(self, script, clock) -> None:
        self._script, self._clock, self._buf = list(script), clock, b''

    async def read(self, n: int = -1) -> bytes:
        await asyncio.sleep(0)
        while not self._buf:
            if not self._script:
                return b''
            gap, data = self._script.pop(0)
            self._clock.now += gap
            self._buf = data
        want = len(self._buf) if n < 0 else min(n, len(self._buf))
        out, self._buf = self._buf[:want], self._buf[want:]
        return out


@pytest.mark.asyncio
async def test_client_min_body_rate_logs(caps_caplog, monkeypatch):
    from types import SimpleNamespace

    from blackbull.client.http1 import HTTP1ResponseRecipient

    monkeypatch.setenv('BB_CLIENT_MIN_BODY_RATE', '1000')
    monkeypatch.setenv('BB_CLIENT_MIN_BODY_RATE_GRACE', '1')
    clock = SimpleNamespace(now=0.0)
    monkeypatch.setattr('blackbull.client.http1._monotonic', lambda: clock.now)

    script = [(0.0, b'HTTP/1.1 200 OK\r\ncontent-length: 100\r\n\r\n')]
    script += [(0.0 if i == 0 else 1.0, b'x' * 10) for i in range(10)]
    with pytest.raises(TimeoutError):
        await HTTP1ResponseRecipient().receive(_DrippingPeer(script, clock))

    records = _records_for(caps_caplog, 'client_min_body_rate')
    assert len(records) >= 1
    assert records[0].levelno == logging.WARNING
    assert records[0].protocol == 'http1'
    assert records[0].limit == 1000.0


# ----------------------------------------------------------------------
# The async client's HTTP/2 response bounds
# ----------------------------------------------------------------------

async def _refused_h2(monkeypatch, env, value, feed):
    """Drive one HTTP/2 client rejection site and return the client."""
    from blackbull.client.http2 import HTTP2Client, _PendingResponse

    monkeypatch.setenv(env, value)
    c = HTTP2Client('localhost', 1)

    async def _sink(_frame):
        pass

    c._control_sender = _sink
    c._responses[1] = _PendingResponse(
        future=asyncio.get_running_loop().create_future())
    await feed(c)
    return c


@pytest.mark.asyncio
async def test_client_body_max_total_logs_on_http2(caps_caplog, monkeypatch):
    from blackbull.protocol.frame_types import FrameTypes

    async def _feed(c):
        frame = c._factory.create(FrameTypes.DATA, 0, 1, data=b'x' * 5000)
        await c._on_response_data(frame)

    await _refused_h2(monkeypatch, 'BB_CLIENT_BODY_MAX_TOTAL', '1000', _feed)
    records = _records_for(caps_caplog, 'client_body_max_total')
    assert records and records[0].protocol == 'http2'


@pytest.mark.asyncio
async def test_client_head_max_total_logs_on_http2(caps_caplog, monkeypatch):
    from blackbull.protocol.frame_types import FrameTypes

    async def _feed(c):
        frame = c._factory.create(FrameTypes.HEADERS, 4, 1)
        frame.headers.extend((f'x-{i}', 'v' * 200) for i in range(50))
        await c._on_response_headers(frame)

    await _refused_h2(monkeypatch, 'BB_CLIENT_HEAD_MAX_TOTAL', '1000', _feed)
    records = _records_for(caps_caplog, 'client_head_max_total')
    assert records and records[0].protocol == 'http2'


@pytest.mark.asyncio
async def test_client_body_timeout_logs_on_http2(caps_caplog, monkeypatch):
    """The per-stream progress deadline — the timer, not a read."""
    from blackbull.protocol.frame_types import FrameTypes

    async def _feed(c):
        from blackbull.protocol.frame_types import PseudoHeaders

        frame = c._factory.create(FrameTypes.HEADERS, 4, 1)
        # A final status: an interim (1xx) head does not start the clock.
        frame.pseudo_headers[PseudoHeaders.STATUS] = '200'
        frame.headers.append(('x-a', 'b'))
        await c._on_response_headers(frame)
        await asyncio.sleep(0.1)

    await _refused_h2(monkeypatch, 'BB_CLIENT_BODY_TIMEOUT', '0.02', _feed)
    records = _records_for(caps_caplog, 'client_body_timeout')
    assert records and records[0].protocol == 'http2'


# ----------------------------------------------------------------------
# client_head_timeout — the HTTP/2 field block's own wait
# ----------------------------------------------------------------------

@pytest.mark.asyncio
async def test_client_head_timeout_logs_on_http2(caps_caplog, monkeypatch):
    """A peer that opens a field block without END_HEADERS and stops.  The
    wait for a frame to *begin* is otherwise unbounded on purpose, so this is
    the one place the head deadline reaches HTTP/2."""
    from blackbull.client.http2 import HTTP2Client
    from blackbull.server.recipient import AbstractReader

    monkeypatch.setenv('BB_CLIENT_HEAD_TIMEOUT', '0.1')

    class _Silent(AbstractReader):
        """A HEADERS without END_HEADERS, then nothing."""

        def __init__(self) -> None:
            self._buf = ((1).to_bytes(3, 'big') + bytes([0x1, 0x0])
                         + (1).to_bytes(4, 'big') + b'\x88')
            self._pos = 0

        async def readexactly(self, n: int) -> bytes:
            while len(self._buf) - self._pos < n:
                await asyncio.sleep(0.005)
            out = self._buf[self._pos:self._pos + n]
            self._pos += n
            return out

        async def read(self, n: int = -1) -> bytes:
            return await self.readexactly(max(n, 0))

    c = HTTP2Client('localhost', 1)
    c._reader = _Silent()

    async def _sink(_frame):
        pass

    c._control_sender = _sink
    await asyncio.wait_for(c._receive_loop(), 2.0)

    records = _records_for(caps_caplog, 'client_head_timeout')
    assert records and records[0].protocol == 'http2'


# ----------------------------------------------------------------------
# client_max_interim_responses — the count axis on the response head
# ----------------------------------------------------------------------

@pytest.mark.asyncio
async def test_client_max_interim_responses_logs(caps_caplog, monkeypatch):
    """A peer that keeps answering ``100 Continue`` passes every size and
    every deadline: each head is small and prompt, and no body is involved.
    Only the count is anomalous."""
    from blackbull.client.exceptions import ResponseTooLarge
    from blackbull.client.http1 import HTTP1ResponseRecipient
    from blackbull.server.recipient import AbstractReader

    monkeypatch.setenv('BB_CLIENT_MAX_INTERIM_RESPONSES', '3')

    class _Interims(AbstractReader):
        def __init__(self) -> None:
            self._buf = b'HTTP/1.1 100 Continue\r\n\r\n' * 40
            self._pos = 0

        async def read(self, n: int = -1) -> bytes:
            out = (self._buf[self._pos:] if n < 0
                   else self._buf[self._pos:self._pos + n])
            self._pos += len(out)
            return out

    with pytest.raises(ResponseTooLarge):
        await HTTP1ResponseRecipient().receive(_Interims())

    records = _records_for(caps_caplog, 'client_max_interim_responses')
    assert records and records[0].protocol == 'http1'
    assert records[0].limit == 3


# ----------------------------------------------------------------------
# client_raw_queue_depth — the raw-stream escape hatch's own queue
# ----------------------------------------------------------------------

@pytest.mark.asyncio
async def test_client_raw_queue_depth_logs_on_http2(caps_caplog, monkeypatch):
    """A raw stream whose registrant does not drain.  Zero-length DATA is the
    shape that reaches it for free: RFC 9113 §6.9.1 charges the payload, so
    flow control never sees the frames pile up."""
    from blackbull.client.http2 import HTTP2Client
    from blackbull.protocol.frame_types import FrameTypes

    monkeypatch.setenv('BB_CLIENT_RAW_QUEUE_DEPTH', '2')
    c = HTTP2Client('localhost', 1)

    async def _sink(_frame):
        pass

    c._control_sender = _sink
    queue = c.register_raw_stream(1)
    for _ in range(2):
        queue.put_nowait(c._factory.create(FrameTypes.DATA, 0, 1, data=b''))
    await c._refuse_raw_stream(
        c._factory.create(FrameTypes.DATA, 0, 1, data=b''))

    records = _records_for(caps_caplog, 'client_raw_queue_depth')
    assert records and records[0].protocol == 'http2'
    assert records[0].limit == 2


# ----------------------------------------------------------------------
# client_h2_max_frame_size — the HTTP/2 frame, refused before it is read
# ----------------------------------------------------------------------

@pytest.mark.asyncio
async def test_client_h2_max_frame_size_logs_on_http2(caps_caplog, monkeypatch):
    """A 9-byte header declaring more than the client's effective
    SETTINGS_MAX_FRAME_SIZE.  The payload is never sent, and never read:
    the record is written from the declared length alone."""
    from blackbull.client.http2 import HTTP2Client, _ConnectionFailed
    from blackbull.protocol.frame_types import FrameTypes
    from blackbull.server.recipient import AbstractReader

    monkeypatch.setenv('BB_CLIENT_H2_MAX_FRAME_SIZE', '1024')

    class _Header(AbstractReader):
        def __init__(self, data: bytes) -> None:
            self._buf = bytearray(data)

        async def readexactly(self, n: int) -> bytes:
            if len(self._buf) < n:
                raise AssertionError('the refused payload was read')
            out = bytes(self._buf[:n])
            del self._buf[:n]
            return out

        async def read(self, n: int = -1) -> bytes:
            return await self.readexactly(max(n, 0))

    c = HTTP2Client('localhost', 1)
    c._reader = _Header((4096).to_bytes(3, 'big') + FrameTypes.DATA
                        + bytes([0]) + (1).to_bytes(4, 'big'))

    async def _sink(_frame):
        pass

    c._control_sender = _sink
    with pytest.raises(_ConnectionFailed):
        await c._receive_frame()

    records = _records_for(caps_caplog, 'client_h2_max_frame_size')
    assert records and records[0].protocol == 'http2'
    assert records[0].limit == 1024
    assert records[0].requested == 4096


# ----------------------------------------------------------------------
# Async client, HTTP/1.1 — the response bounds that refused in silence
#
# Every rejection below already happened before it kept a record; what
# these tests gate is the record.  All of them drive the real site from a
# fake reader rather than calling ``log_cap_hit`` — the preference this
# file's docstring states — because no client bound needs a live
# connection to reach.
# ----------------------------------------------------------------------

class _ClientCanned(AbstractReader):
    """A peer that says its piece and then reports EOF."""

    def __init__(self, payload: bytes) -> None:
        self._buf, self._pos = payload, 0

    async def read(self, n: int = -1) -> bytes:
        out = (self._buf[self._pos:] if n < 0
               else self._buf[self._pos:self._pos + n])
        self._pos += len(out)
        return out


class _ClientStalling(AbstractReader):
    """A peer that sends *prefix* and then never sends anything again."""

    def __init__(self, prefix: bytes) -> None:
        self._prefix, self._pos = prefix, 0

    async def read(self, n: int = -1) -> bytes:
        if self._pos < len(self._prefix):
            end = len(self._prefix) if n < 0 else self._pos + n
            out = self._prefix[self._pos:end]
            self._pos += len(out)
            return out
        await asyncio.Event().wait()
        raise AssertionError('unreachable')


class _ClientRaisesTimeout(AbstractReader):
    """A reader whose own read raises ``TimeoutError`` promptly.

    Not the deadline expiring — something under the transport answering in
    the same exception type.  The negative control for both time bounds.
    """

    def __init__(self, prefix: bytes = b'') -> None:
        self._buf, self._pos = prefix, 0

    async def read(self, n: int = -1) -> bytes:
        if self._pos < len(self._buf):
            end = len(self._buf) if n < 0 else self._pos + n
            out = self._buf[self._pos:end]
            self._pos += len(out)
            return out
        raise TimeoutError('the transport said so, not the cap')


def _h1_response(*fields: bytes) -> bytes:
    """A 200 head with *fields* already CRLF-terminated."""
    return b'HTTP/1.1 200 OK\r\n' + b''.join(fields) + b'\r\n'


def _h1_chunked(body: bytes) -> bytes:
    return b'HTTP/1.1 200 OK\r\ntransfer-encoding: chunked\r\n\r\n' + body


@pytest.mark.asyncio
async def test_client_head_max_total_logs_on_http1(caps_caplog, monkeypatch):
    """Forty field lines, each far under the per-line cap.  Only the sum is
    anomalous, so the total is the only bound that can refuse it."""
    from blackbull.client.exceptions import ResponseTooLarge
    from blackbull.client.http1 import HTTP1ResponseRecipient

    monkeypatch.setenv('BB_CLIENT_HEAD_MAX_TOTAL', '512')
    head = _h1_response(*[b'x-pad-%03d: %s\r\n' % (i, b'v' * 40)
                          for i in range(40)])
    with pytest.raises(ResponseTooLarge):
        await HTTP1ResponseRecipient().receive(_ClientCanned(head))

    records = _records_for(caps_caplog, 'client_head_max_total')
    assert records and records[0].protocol == 'http1'
    assert records[0].limit == 512
    assert records[0].requested > 512


@pytest.mark.asyncio
async def test_client_head_max_total_logs_on_http1_trailers(
        caps_caplog, monkeypatch):
    """The second place the head budget is spent: the trailer section is
    field lines by another name, and it accumulates after the body."""
    from blackbull.client.exceptions import ResponseTooLarge
    from blackbull.client.http1 import HTTP1ResponseRecipient

    monkeypatch.setenv('BB_CLIENT_HEAD_MAX_TOTAL', '64')
    monkeypatch.setenv('BB_CLIENT_HEAD_MAX_LINE', '4096')
    trailers = b''.join(b'x-t-%03d: %s\r\n' % (i, b'v' * 40) for i in range(8))
    reader = _ClientCanned(_h1_chunked(b'2\r\nhi\r\n0\r\n' + trailers + b'\r\n'))
    with pytest.raises(ResponseTooLarge):
        await HTTP1ResponseRecipient().receive(reader)

    records = _records_for(caps_caplog, 'client_head_max_total')
    assert records and records[0].protocol == 'http1'
    assert records[0].limit == 64
    assert records[0].requested > 64


@pytest.mark.asyncio
async def test_client_head_max_line_logs_on_http1(caps_caplog, monkeypatch):
    """One field line over the per-line rule inside a head well under the
    total, so the line cap is the only bound that sees it."""
    from blackbull.client.exceptions import ResponseTooLarge
    from blackbull.client.http1 import HTTP1ResponseRecipient

    monkeypatch.setenv('BB_CLIENT_HEAD_MAX_TOTAL', '8192')
    monkeypatch.setenv('BB_CLIENT_HEAD_MAX_LINE', '64')
    reader = _ClientCanned(_h1_response(b'x-big: ' + b'v' * 200 + b'\r\n'))
    with pytest.raises(ResponseTooLarge):
        await HTTP1ResponseRecipient().receive(reader)

    records = _records_for(caps_caplog, 'client_head_max_line')
    assert records and records[0].protocol == 'http1'
    assert records[0].limit == 64
    assert records[0].requested > 64


@pytest.mark.asyncio
async def test_client_head_max_line_logs_on_http1_framing_line(
        caps_caplog, monkeypatch):
    """A chunk-size line padded with extensions.  The same cap bounds it,
    and it is read during the body rather than during the head.

    The budget is set above every line of the head on purpose: at a value
    the head itself breaches, the head walk refuses first and this test
    would pass while the framing site stayed unwired.
    """
    from blackbull.client.exceptions import ResponseTooLarge
    from blackbull.client.http1 import HTTP1ResponseRecipient

    monkeypatch.setenv('BB_CLIENT_HEAD_MAX_LINE', '32')
    reader = _ClientCanned(
        _h1_chunked(b'2;' + b'e' * 200 + b'\r\nhi\r\n0\r\n\r\n'))
    with pytest.raises(ResponseTooLarge):
        await HTTP1ResponseRecipient().receive(reader)

    records = _records_for(caps_caplog, 'client_head_max_line')
    assert records and records[0].protocol == 'http1'
    assert records[0].limit == 32
    assert records[0].requested > 32


@pytest.mark.asyncio
async def test_client_head_timeout_logs_on_http1(caps_caplog, monkeypatch):
    """Half a head and then silence: every byte budget is satisfied
    forever, so only the deadline can end it."""
    from blackbull.client.http1 import HTTP1ResponseRecipient

    monkeypatch.setenv('BB_CLIENT_HEAD_TIMEOUT', '0.05')
    reader = _ClientStalling(b'HTTP/1.1 200 OK\r\nx-half: ')
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(HTTP1ResponseRecipient().receive(reader), 3.0)

    records = _records_for(caps_caplog, 'client_head_timeout')
    assert records and records[0].protocol == 'http1'
    assert records[0].limit == 0.05


@pytest.mark.asyncio
async def test_client_head_timeout_is_not_logged_for_a_reader_s_own_timeout(
        caps_caplog, monkeypatch):
    """A ``TimeoutError`` that reached the client from *inside* the reader
    is not this cap refusing, and a record naming it would report a refusal
    that never happened.  The deadline here is long enough that only the
    reader can have raised."""
    from blackbull.client.http1 import HTTP1ResponseRecipient

    monkeypatch.setenv('BB_CLIENT_HEAD_TIMEOUT', '30.0')
    reader = _ClientRaisesTimeout(b'HTTP/1.1 200 OK\r\nx-half: ')
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(HTTP1ResponseRecipient().receive(reader), 3.0)

    assert not _records_for(caps_caplog, 'client_head_timeout')


@pytest.mark.asyncio
async def test_client_body_timeout_logs_on_http1(caps_caplog, monkeypatch):
    """A complete head, two octets of a ten-octet body, then silence."""
    from blackbull.client.http1 import HTTP1ResponseRecipient

    monkeypatch.setenv('BB_CLIENT_BODY_TIMEOUT', '0.05')
    reader = _ClientStalling(_h1_response(b'content-length: 10\r\n') + b'ab')
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(HTTP1ResponseRecipient().receive(reader), 3.0)

    records = _records_for(caps_caplog, 'client_body_timeout')
    assert records and records[0].protocol == 'http1'
    assert records[0].limit == 0.05


@pytest.mark.asyncio
async def test_client_body_timeout_is_not_logged_for_a_reader_s_own_timeout(
        caps_caplog, monkeypatch):
    """The body half of the control above."""
    from blackbull.client.http1 import HTTP1ResponseRecipient

    monkeypatch.setenv('BB_CLIENT_BODY_TIMEOUT', '30.0')
    reader = _ClientRaisesTimeout(
        _h1_response(b'content-length: 10\r\n') + b'ab')
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(HTTP1ResponseRecipient().receive(reader), 3.0)

    assert not _records_for(caps_caplog, 'client_body_timeout')


@pytest.mark.asyncio
async def test_client_body_max_total_logs_on_http1_declared(
        caps_caplog, monkeypatch):
    """Refused on the ``Content-Length`` before an octet is read, so the
    record is written from the declaration alone."""
    from blackbull.client.exceptions import ResponseTooLarge
    from blackbull.client.http1 import HTTP1ResponseRecipient

    monkeypatch.setenv('BB_CLIENT_BODY_MAX_TOTAL', '8')
    reader = _ClientCanned(_h1_response(b'content-length: 64\r\n') + b'x' * 64)
    with pytest.raises(ResponseTooLarge):
        await HTTP1ResponseRecipient().receive(reader)

    records = _records_for(caps_caplog, 'client_body_max_total')
    assert records and records[0].protocol == 'http1'
    assert records[0].limit == 8
    assert records[0].requested == 64


@pytest.mark.asyncio
async def test_client_body_max_total_logs_on_http1_chunked(
        caps_caplog, monkeypatch):
    """A chunked body declares nothing up front, so the same cap is spent
    against the running total instead."""
    from blackbull.client.exceptions import ResponseTooLarge
    from blackbull.client.http1 import HTTP1ResponseRecipient

    monkeypatch.setenv('BB_CLIENT_BODY_MAX_TOTAL', '8')
    reader = _ClientCanned(_h1_chunked(b'40\r\n' + b'x' * 64 + b'\r\n0\r\n\r\n'))
    with pytest.raises(ResponseTooLarge):
        await HTTP1ResponseRecipient().receive(reader)

    records = _records_for(caps_caplog, 'client_body_max_total')
    assert records and records[0].protocol == 'http1'
    assert records[0].limit == 8
    assert records[0].requested == 64


@pytest.mark.asyncio
async def test_client_body_max_total_logs_on_http1_close_delimited(
        caps_caplog, monkeypatch):
    """The instrument's positive control.

    This site kept a record before this file's other client tests existed,
    so it is the one row that must pass whether or not the wiring under
    test is present.  A run where every client assertion here goes green
    while this one is silent is not evidence about the caps — it is
    evidence that the harness never reached them.
    """
    from blackbull.client.exceptions import ResponseTooLarge
    from blackbull.client.http1 import HTTP1ResponseRecipient

    monkeypatch.setenv('BB_CLIENT_BODY_MAX_TOTAL', '8')
    reader = _ClientCanned(
        b'HTTP/1.1 200 OK\r\nconnection: close\r\n\r\n' + b'x' * 64)
    with pytest.raises(ResponseTooLarge):
        await HTTP1ResponseRecipient().receive(reader)

    records = _records_for(caps_caplog, 'client_body_max_total')
    assert records and records[0].protocol == 'http1'
    assert records[0].limit == 8


# ----------------------------------------------------------------------
# client_h2_max_header_list_size — the decoded field section
# ----------------------------------------------------------------------

def _h2_headers_frame(block: bytes, *, stream_id: int = 1) -> bytes:
    from blackbull.protocol.frame_types import FrameTypes, HeaderFrameFlags
    flags = int(HeaderFrameFlags.END_HEADERS) | int(HeaderFrameFlags.END_STREAM)
    return (len(block).to_bytes(3, 'big') + FrameTypes.HEADERS.value
            + bytes([flags]) + stream_id.to_bytes(4, 'big') + block)


class _H2Canned(AbstractReader):
    """Feeds queued frame bytes, then EOF."""

    def __init__(self, data: bytes) -> None:
        self._buf = bytearray(data)

    async def readexactly(self, n: int) -> bytes:
        if len(self._buf) < n:
            raise asyncio.IncompleteReadError(bytes(self._buf), n)
        out = bytes(self._buf[:n])
        del self._buf[:n]
        return out

    async def read(self, n: int = -1) -> bytes:
        return await self.readexactly(max(n, 0))


async def _h2_client_reading(frame: bytes):
    """A client whose next frame read is *frame*, with a GOAWAY sink."""
    from blackbull.client.http2 import HTTP2Client
    c = HTTP2Client('localhost', 1)
    c._reader = _H2Canned(frame)
    sent: list = []

    async def _sink(f):
        sent.append(f)

    c._control_sender = _sink
    return c, sent


@pytest.mark.asyncio
async def test_client_h2_max_header_list_size_logs_on_http2(
        caps_caplog, monkeypatch):
    """A field section over the advertised decoded total.

    ``requested`` is a lower bound rather than the figure, because hpack
    reports the limit it refused at and never the total it reached.  A
    tight one: it charges each entry and compares immediately, so it
    raises on the entry that crosses and the section is provably *just*
    over.  What is gated below is therefore the property — over the limit,
    in the limit's own unit — and not the constant.
    """
    from hpack import Encoder
    from blackbull.client.http2 import _ConnectionFailed
    from blackbull.protocol.frame_types import ErrorCodes, FrameTypes

    monkeypatch.setenv('BB_CLIENT_H2_MAX_HEADER_LIST_SIZE', '2048')
    fields, charged, i = [], 0, 0
    while charged <= 2048:
        name, value = f'x-pad-{i}', 'v' * 64
        fields.append((name, value))
        charged += len(name) + len(value) + 32
        i += 1
    block = Encoder().encode(fields)

    c, sent = await _h2_client_reading(_h2_headers_frame(block))
    with pytest.raises(_ConnectionFailed):
        await c._receive_frame()

    goaway = [f for f in sent if f.FrameType() == FrameTypes.GOAWAY]
    assert goaway and goaway[0].error_code == ErrorCodes.COMPRESSION_ERROR
    records = _records_for(caps_caplog, 'client_h2_max_header_list_size')
    assert records and records[0].protocol == 'http2'
    assert records[0].limit == 2048
    assert records[0].requested > records[0].limit


@pytest.mark.asyncio
async def test_client_h2_max_header_list_size_is_not_logged_for_a_bad_block(
        caps_caplog, monkeypatch):
    """An HPACK block that is simply invalid ends the connection the same
    way, and is not this cap.  Index 0 is not a table entry, so the peer's
    own error must not be filed under our bound."""
    from blackbull.client.http2 import _ConnectionFailed
    from blackbull.protocol.frame_types import ErrorCodes, FrameTypes

    monkeypatch.setenv('BB_CLIENT_H2_MAX_HEADER_LIST_SIZE', '2048')
    c, sent = await _h2_client_reading(_h2_headers_frame(b'\x80'))
    with pytest.raises(_ConnectionFailed):
        await c._receive_frame()

    goaway = [f for f in sent if f.FrameType() == FrameTypes.GOAWAY]
    assert goaway and goaway[0].error_code == ErrorCodes.COMPRESSION_ERROR
    assert not _records_for(caps_caplog, 'client_h2_max_header_list_size')


# ----------------------------------------------------------------------
# Client wiring audit — the unit is the (cap, protocol) pair
#
# By name alone the client looked wired: six caps appeared in some
# ``log_cap_hit`` call under ``blackbull/`` while every HTTP/1.1 site was
# silent, because the HTTP/2 sites carried the name.  A cap enforced on
# two protocols is two rejection sites and needs two records, so the
# audit's unit is the pair.
#
# ``_CLIENT_CAPS`` is checked in both directions against the call sites,
# and the two declarations together are checked against ``env.py``, so a
# ``BB_CLIENT_*`` var added without a verdict fails here on the day it
# lands rather than at the next audit someone happens to run.
# ----------------------------------------------------------------------

#: Client cap → the protocols whose rejection sites must name it.
_CLIENT_CAPS: dict[str, frozenset[str]] = {
    'client_body_max_total':          frozenset({'http1', 'http2'}),
    'client_body_timeout':            frozenset({'http1', 'http2'}),
    'client_h2_max_frame_size':       frozenset({'http2'}),
    'client_h2_max_header_list_size': frozenset({'http2'}),
    'client_head_max_line':           frozenset({'http1'}),
    'client_head_max_total':          frozenset({'http1', 'http2'}),
    'client_head_timeout':            frozenset({'http1', 'http2'}),
    'client_max_interim_responses':   frozenset({'http1'}),
    'client_min_body_rate':           frozenset({'http1'}),
    'client_raw_queue_depth':         frozenset({'http2'}),
}

#: ``BB_CLIENT_*`` vars that are not caps, and why.  Declared rather than
#: omitted: silence here is indistinguishable from an oversight, which is
#: how the header-list-size site shipped unwired.
_CLIENT_NOT_A_CAP: dict[str, str] = {
    'client_h2_enable_push':
        'a conformance switch (RFC 9113 §6.5.2), not a bound — it refuses '
        'nothing on its own, and a promise costs what a HEADERS frame costs',
    'client_min_body_rate_grace':
        'a grace-period modifier of client_min_body_rate, which owns the '
        'refusal and the record',
}

#: Protocols an HTTP/1.1-only cap is *not* expected on, and vice versa.
#: Absences that are deliberate, so the equality above is a decision and
#: not a snapshot:
#:
#: - ``client_max_interim_responses`` has no HTTP/2 site because each
#:   interim HEADERS block adds to the same ``headers_seen`` total as the
#:   final one, so ``client_head_max_total`` already owns the aggregate.
#: - ``client_head_max_line`` has no HTTP/2 site because HTTP/2 has no
#:   field *line* — the section is the unit, bounded by
#:   ``client_h2_max_header_list_size``.
#: - ``client_min_body_rate`` has no HTTP/2 site because HTTP/2 has no
#:   rate floor at all.  That is a missing bound, not a missing record.


def _client_source_paths() -> list:
    from pathlib import Path
    root = Path(__file__).resolve().parents[2] / 'blackbull' / 'client'
    return sorted(p for p in root.glob('*.py'))


def _enclosing_function(node, parents):
    cur = parents.get(node)
    while cur is not None and not isinstance(
            cur, (ast.FunctionDef, ast.AsyncFunctionDef)):
        cur = parents.get(cur)
    return cur


def _forwarded_constants(param: str, func, tree) -> list[str]:
    """The constants *func*'s own callers pass in *param*.

    One level of forwarding, which is all the client has: the HTTP/2
    client's three per-stream bounds share ``_refuse_stream`` and pass the
    cap name to it, so the literal never appears beside ``log_cap_hit``.
    Refusing to resolve it would make this audit an argument for copying
    the helper.
    """
    positional = [a.arg for a in func.args.posonlyargs + func.args.args]
    index = None
    if param in positional:
        index = positional.index(param)
        if positional[0] in ('self', 'cls'):
            index -= 1          # bound calls do not pass the receiver
    elif param not in [a.arg for a in func.args.kwonlyargs]:
        return []
    found = []
    for call in ast.walk(tree):
        if not isinstance(call, ast.Call):
            continue
        callee = call.func
        name = (callee.attr if isinstance(callee, ast.Attribute)
                else callee.id if isinstance(callee, ast.Name) else None)
        if name != func.name:
            continue
        for kw in call.keywords:
            if kw.arg == param and isinstance(kw.value, ast.Constant):
                found.append(kw.value.value)
        if index is not None and 0 <= index < len(call.args):
            arg = call.args[index]
            if isinstance(arg, ast.Constant):
                found.append(arg.value)
    return found


def _observed_client_pairs() -> set:
    """Every ``(cap, protocol)`` a ``log_cap_hit`` call under
    ``blackbull/client/`` can produce, read out of the source."""
    pairs = set()
    for path in _client_source_paths():
        tree = ast.parse(path.read_text())
        parents = {child: node for node in ast.walk(tree)
                   for child in ast.iter_child_nodes(node)}
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Name)
                    and node.func.id == 'log_cap_hit'):
                continue
            protocol = None
            for kw in node.keywords:
                if kw.arg == 'protocol' and isinstance(kw.value, ast.Constant):
                    protocol = kw.value.value
            first = node.args[0] if node.args else None
            where = f'{path.name}:{node.lineno}'
            if isinstance(first, ast.Constant):
                caps = [first.value]
            elif isinstance(first, ast.Name):
                func = _enclosing_function(node, parents)
                assert func is not None, (
                    f'{where}: log_cap_hit outside a function')
                caps = _forwarded_constants(first.id, func, tree)
                assert caps, (
                    f'{where}: log_cap_hit({first.id}) forwards a cap name '
                    f'this audit cannot resolve — no caller of '
                    f'{func.name}() passes a literal')
            else:
                raise AssertionError(
                    f'{where}: log_cap_hit called with a cap name that is '
                    f'neither a literal nor a forwarded parameter')
            for cap in caps:
                pairs.add((cap, protocol))
    return pairs


def test_client_caps_are_wired_on_every_protocol_that_enforces_them():
    """Both directions.  A declared pair with no call site is a cap that
    refuses in silence; a call site with an undeclared pair is a record
    nobody decided to keep."""
    declared = {(cap, protocol)
                for cap, protocols in _CLIENT_CAPS.items()
                for protocol in protocols}
    observed = _observed_client_pairs()
    assert observed == declared, (
        f'unwired (declared, no call site): {sorted(declared - observed)}; '
        f'undeclared (call site, no declaration): {sorted(observed - declared)}')


def test_every_client_env_var_has_a_verdict():
    """The check that would have caught the header-list-size site the day
    it shipped: a new ``BB_CLIENT_*`` var is either a cap with protocols or
    a declared non-cap, and there is no third answer."""
    from pathlib import Path
    env_path = Path(__file__).resolve().parents[2] / 'blackbull' / 'env.py'
    names = {node.value[len('BB_'):].lower()
             for node in ast.walk(ast.parse(env_path.read_text()))
             if isinstance(node, ast.Constant) and isinstance(node.value, str)
             and node.value.startswith('BB_CLIENT_')}
    assert names, 'no BB_CLIENT_* vars found — the reader, not the code, broke'
    declared = set(_CLIENT_CAPS) | set(_CLIENT_NOT_A_CAP)
    assert names == declared, (
        f'undeclared env vars: {sorted(names - declared)}; '
        f'declared but not in env.py: {sorted(declared - names)}')


def test_every_declared_client_cap_is_a_settings_field():
    """The env-var spelling and the ``Settings`` field must agree, or the
    two audits above pass while naming something no code reads."""
    from blackbull.env import get_settings
    settings = get_settings()
    missing = [name for name in set(_CLIENT_CAPS) | set(_CLIENT_NOT_A_CAP)
               if not hasattr(settings, name)]
    assert not missing, f'not Settings fields: {sorted(missing)}'


# ----------------------------------------------------------------------
# Wiring audit — every cap in the inventory appears at >= 1 log_cap_hit()
# call in the codebase.  Static check; catches "removed wiring without
# noticing" regressions even when a functional test happens to skip.
# ----------------------------------------------------------------------

_INVENTORY = (
    'max_connections',
    'header_timeout',
    'header_max_line',
    'header_max_total',
    'body_timeout',
    'request_timeout',
    'write_timeout',
    'ws_max_frame_payload',
    'stream_queue_depth',
    'h2_inbound_window_budget',
    'h2_max_concurrent_streams',
    'h2_ws_max_streams_per_connection',
    'compression_max_inflight',
)


@pytest.mark.parametrize('cap', _INVENTORY + tuple(sorted(_CLIENT_CAPS)))
def test_cap_present_in_codebase(cap):
    """Static audit — every inventory cap must appear in at least one
    ``log_cap_hit('<cap>', ...)`` call under ``blackbull/``.  Cheap and
    catches the developer-forgot-to-wire mistake even when a functional
    test would silently skip.

    The name and the call need not be on the same line.  A rejection site that
    shares one refusal helper — as the HTTP/2 client's three bounds do — passes
    the cap name *to the helper*, so the literal ``log_cap_hit('<cap>'`` never
    appears.  Requiring that spelling would make this audit an argument for
    copying the helper; what it requires instead is the name and a
    ``log_cap_hit`` call in the same file, with the name spelled as a call
    argument — which is how both the direct sites and the forwarded ones
    write it, and which a mention in prose is not."""
    from pathlib import Path
    root = Path(__file__).resolve().parents[2] / 'blackbull'
    hits = [
        p for p in root.rglob('*.py')
        if p.is_file() and f"'{cap}'," in (text := p.read_text())
        and 'log_cap_hit' in text
    ]
    assert hits, f'{cap!r} not wired in any blackbull/ file'
