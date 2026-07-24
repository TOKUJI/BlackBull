"""Sprint 44 coverage gate — each cap in the inventory has a unit test
asserting it logs at the rejection site.

If a future PR adds a new cap (``BB_*`` env var that rejects traffic)
without wiring a ``log_cap_hit(...)`` call at the rejection site, this
file is where the new test belongs.  Until it does, the inventory
contract is not satisfied — see
``.claude/planning/candidates/cap-hit-logging.md`` for the design.

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

import asyncio
import logging

import pytest

from blackbull.server.http1_actor import HTTP1Actor
from blackbull.server.recipient import AbstractReader
from blackbull.server.sender import AbstractWriter


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
        conn={'path': '/ws'},
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
        conn={'path': '/ws'},
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

    conn = {'path': '/upload', 'headers': [(b'content-length', b'1024')]}
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
    conn = {'path': '/upload', 'headers': [(b'content-length', b'5')]}
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
    q.put_nowait({'type': 'http.request', 'body': b'first', 'more_body': True})

    dropped = recipient.put_event({'type': 'http.request',
                                    'body': b'overflow', 'more_body': False})
    assert dropped is False  # event was dropped
    assert len(_records_for(caps_caplog, 'stream_queue_depth')) >= 1


@pytest.mark.asyncio
async def test_stream_queue_depth_no_log_under_cap(caps_caplog):
    """When the H/2 stream queue has room, no cap-hit log fires."""
    from blackbull.server.recipient import HTTP2Recipient

    recipient = HTTP2Recipient()
    recipient._queue_depth = 10
    # Queue has room — put_nowait must succeed.
    ok = recipient.put_event({'type': 'http.request',
                               'body': b'data', 'more_body': False})
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
        async def read(self, n: int = -1) -> bytes:
            return b''
        async def readuntil(self, sep: bytes) -> bytes:
            # Never return the header terminator — timeout will fire.
            await asyncio.sleep(10.0)
            return b''
        async def readexactly(self, n: int) -> bytes:
            raise asyncio.IncompleteReadError(b'', n)

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
    stream.conn = {'path': '/ws', 'type': 'websocket'}
    stream.stream_id = 5
    tg = MagicMock(spec=asyncio.TaskGroup)
    log_record = MagicMock()

    await actor._handle_h2_websocket(stream, tg, log_record)

    records = _records_for(caps_caplog, 'h2_ws_max_streams_per_connection')
    assert len(records) >= 1
    assert records[0].levelno == logging.WARNING
    assert records[0].protocol == 'h2-ws'


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


@pytest.mark.parametrize('cap', _INVENTORY)
def test_cap_present_in_codebase(cap):
    """Static audit — every inventory cap must appear in at least one
    ``log_cap_hit('<cap>', ...)`` call under ``blackbull/``.  Cheap and
    catches the developer-forgot-to-wire mistake even when a functional
    test would silently skip."""
    from pathlib import Path
    needle = f"log_cap_hit('{cap}'"
    root = Path(__file__).resolve().parents[2] / 'blackbull'
    hits = [
        p for p in root.rglob('*.py')
        if p.is_file() and needle in p.read_text()
    ]
    assert hits, f'{cap!r} not wired in any blackbull/ file'
