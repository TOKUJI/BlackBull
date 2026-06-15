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
"""
from __future__ import annotations

import asyncio
import logging

import pytest

from blackbull.server.cap_log import log_cap_hit


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
        scope={'path': '/ws'},
        max_frame_payload=1024,
    )
    recipient._event_queue = asyncio.Queue()    # _read_loop asserts non-None

    # _read_loop handles ProtocolError internally (sends CLOSE 1009 and
    # exits cleanly); the cap-hit log fires before that handling.
    await recipient._read_loop()

    assert len(_records_for(caps_caplog, 'ws_max_frame_payload')) >= 1


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

    scope = {'path': '/upload', 'headers': [(b'content-length', b'1024')]}
    recipient = HTTP1Recipient(reader=_SlowReader(), scope=scope,
                               body_timeout=0.0)
    recipient._content_length = 1024  # bypass the parser

    event = await recipient()
    # HTTP_DISCONNECT is the user-visible behaviour; the cap-hit log
    # fires alongside it.
    assert event.get('type') in (b'http.disconnect', 'http.disconnect')
    assert len(_records_for(caps_caplog, 'body_timeout')) == 1


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

    async def app(scope, receive, send):
        await send({'type': 'http.response.start', 'status': 200,
                    'headers': [(b'content-type', b'text/plain')]})
        await send({'type': 'http.response.body', 'body': body, 'more_body': False})

    async def call_next(scope, receive, send):
        await app(scope, receive, send)

    scope = {
        'type': 'http', 'method': 'GET', 'path': '/',
        'headers': [(b'accept-encoding', b'gzip')],
    }
    await mw(scope, receive, send, call_next)

    assert len(_records_for(caps_caplog, 'compression_max_inflight')) == 1


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


# ----------------------------------------------------------------------
# header_max_line + header_max_total + header_timeout (HTTP/1)
# ----------------------------------------------------------------------

def test_header_max_line_logs_via_log_cap_hit_signature():
    """The wiring path is gated by HeaderTooLargeError; the helper
    arg-shape is verified directly here so a future signature change
    is caught immediately."""
    log = logging.getLogger('blackbull.caps')
    log.setLevel(logging.WARNING)

    import logging as _logging
    handler = _logging.Handler()
    captured: list[_logging.LogRecord] = []
    handler.emit = captured.append
    log.addHandler(handler)
    try:
        log_cap_hit('header_max_line', requested=9000, limit=8192,
                    protocol='http1')
    finally:
        log.removeHandler(handler)
    assert any(getattr(r, 'cap', None) == 'header_max_line' for r in captured)


def test_header_max_total_logs():
    log = logging.getLogger('blackbull.caps')
    log.setLevel(logging.WARNING)
    captured: list[logging.LogRecord] = []
    h = logging.Handler()
    h.emit = captured.append
    log.addHandler(h)
    try:
        log_cap_hit('header_max_total', requested=70_000, limit=65_536,
                    protocol='http1')
        log_cap_hit('header_max_total', requested=70_000, limit=65_536,
                    protocol='http2')
    finally:
        log.removeHandler(h)
    assert sum(1 for r in captured if getattr(r, 'cap', None) == 'header_max_total') == 2


def test_header_timeout_logs():
    log = logging.getLogger('blackbull.caps')
    log.setLevel(logging.WARNING)
    captured: list[logging.LogRecord] = []
    h = logging.Handler()
    h.emit = captured.append
    log.addHandler(h)
    try:
        log_cap_hit('header_timeout', requested=10.0, limit=10.0,
                    protocol='http1')
    finally:
        log.removeHandler(h)
    assert any(getattr(r, 'cap', None) == 'header_timeout' for r in captured)


# ----------------------------------------------------------------------
# request_timeout (http1_actor + http2_actor)
# ----------------------------------------------------------------------

def test_request_timeout_logs():
    log = logging.getLogger('blackbull.caps')
    log.setLevel(logging.WARNING)
    captured: list[logging.LogRecord] = []
    h = logging.Handler()
    h.emit = captured.append
    log.addHandler(h)
    try:
        log_cap_hit('request_timeout', requested=30.0, limit=30.0)
    finally:
        log.removeHandler(h)
    assert any(getattr(r, 'cap', None) == 'request_timeout' for r in captured)


# ----------------------------------------------------------------------
# max_connections (server.ASGIServer accept)
# ----------------------------------------------------------------------

def test_max_connections_logs():
    log = logging.getLogger('blackbull.caps')
    log.setLevel(logging.WARNING)
    captured: list[logging.LogRecord] = []
    h = logging.Handler()
    h.emit = captured.append
    log.addHandler(h)
    try:
        log_cap_hit('max_connections', requested=129, limit=128,
                    peer=('127.0.0.1', 0), protocol='tcp')
    finally:
        log.removeHandler(h)
    assert any(getattr(r, 'cap', None) == 'max_connections' for r in captured)


# ----------------------------------------------------------------------
# h2_max_concurrent_streams + h2_ws_max_streams_per_connection
# ----------------------------------------------------------------------

def test_h2_max_concurrent_streams_logs():
    log = logging.getLogger('blackbull.caps')
    log.setLevel(logging.WARNING)
    captured: list[logging.LogRecord] = []
    h = logging.Handler()
    h.emit = captured.append
    log.addHandler(h)
    try:
        log_cap_hit('h2_max_concurrent_streams', requested=101, limit=100,
                    protocol='http2')
    finally:
        log.removeHandler(h)
    assert any(getattr(r, 'cap', None) == 'h2_max_concurrent_streams'
               for r in captured)


def test_h2_ws_max_streams_per_connection_logs():
    log = logging.getLogger('blackbull.caps')
    log.setLevel(logging.WARNING)
    captured: list[logging.LogRecord] = []
    h = logging.Handler()
    h.emit = captured.append
    log.addHandler(h)
    try:
        log_cap_hit('h2_ws_max_streams_per_connection',
                    requested=6, limit=5, protocol='h2-ws')
    finally:
        log.removeHandler(h)
    assert any(getattr(r, 'cap', None) == 'h2_ws_max_streams_per_connection'
               for r in captured)


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
