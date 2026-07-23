"""Tests for HTTP2Actor and StreamActor (Phase 6 Step 4)."""
import asyncio
import pytest
from unittest.mock import AsyncMock
from hpack import Encoder

from blackbull.event_aggregator import EventAggregator
from blackbull.protocol.frame import FrameFactory
from blackbull.protocol.frame_types import (
    FrameTypes, HeaderFrameFlags, DEFAULT_INITIAL_WINDOW_SIZE,
)
from blackbull.server.recipient import AbstractReader
from blackbull.server.sender import AbstractWriter
from blackbull.server.http2_actor import HTTP2Actor


# ---------------------------------------------------------------------------
# Wire-format helpers
# ---------------------------------------------------------------------------

def _make_h2_frame(type_byte: FrameTypes, flags: int,
                   stream_id: int, payload: bytes) -> bytes:
    length = len(payload)
    return (length.to_bytes(3, 'big')
            + type_byte                     # FrameTypes is a bytes enum
            + bytes([flags])
            + stream_id.to_bytes(4, 'big')
            + payload)


def _make_headers_frame(stream_id: int = 1, end_stream: bool = True,
                        method: bytes = b'GET', path: bytes = b'/') -> bytes:
    encoder = Encoder()
    block = encoder.encode([(b':method', method),
                             (b':path', path),
                             (b':scheme', b'https'), (b':authority', b'example.com')])
    flags = HeaderFrameFlags.END_HEADERS
    if end_stream:
        flags |= HeaderFrameFlags.END_STREAM
    return _make_h2_frame(FrameTypes.HEADERS, flags, stream_id, block)


# ---------------------------------------------------------------------------
# In-process fakes
# ---------------------------------------------------------------------------

class _FakeReader(AbstractReader):
    def __init__(self, data: bytes):
        self._buf = bytearray(data)

    async def read(self, n: int) -> bytes:
        chunk = bytes(self._buf[:n])
        del self._buf[:n]
        return chunk

    async def readuntil(self, sep: bytes) -> bytes:
        idx = self._buf.find(sep)
        if idx == -1:
            raise asyncio.IncompleteReadError(bytes(self._buf), None)
        chunk = bytes(self._buf[:idx + len(sep)])
        del self._buf[:idx + len(sep)]
        return chunk

    async def readexactly(self, n: int) -> bytes:
        if len(self._buf) < n:
            raise asyncio.IncompleteReadError(bytes(self._buf), n)
        chunk = bytes(self._buf[:n])
        del self._buf[:n]
        return chunk


class _FakeWriter(AbstractWriter):
    def __init__(self):
        self.written = bytearray()

    async def write(self, data: bytes) -> None:
        self.written += data


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def fake_h2_reader():
    """Single GET request (HEADERS + END_STREAM), then EOF."""
    return _FakeReader(_make_headers_frame(stream_id=1, end_stream=True))


@pytest.fixture
def fake_two_stream_reader():
    """Two sequential GET requests on streams 1 and 3, then EOF."""
    return _FakeReader(
        _make_headers_frame(stream_id=1, end_stream=True) +
        _make_headers_frame(stream_id=3, end_stream=True)
    )


@pytest.fixture
def fake_writer():
    return _FakeWriter()


@pytest.fixture
def mock_app():
    async def app(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b""})
    return AsyncMock(side_effect=app)


# ---------------------------------------------------------------------------
# Test 1: single stream → the app is dispatched once; no error event.
# (Request-lifecycle Level B events are emitted by BlackBull._dispatch since
# Sprint 64 — the actor layer only calls the app and reports errors.)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_stream_request_lifecycle(fake_h2_reader, fake_writer, mock_app) -> None:
    aggregator = AsyncMock(spec=EventAggregator)
    actor = HTTP2Actor(fake_h2_reader, fake_writer, mock_app, aggregator)
    await actor.run()

    mock_app.assert_awaited_once()
    aggregator.on_error.assert_not_called()


# ---------------------------------------------------------------------------
# Test 2: two concurrent streams both complete
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_two_concurrent_streams(fake_two_stream_reader, fake_writer, mock_app) -> None:
    aggregator = AsyncMock(spec=EventAggregator)
    actor = HTTP2Actor(fake_two_stream_reader, fake_writer, mock_app, aggregator)
    await actor.run()
    assert mock_app.await_count == 2


# ---------------------------------------------------------------------------
# Test 3: stream error → RST_STREAM sent, other stream completes
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_stream_error_isolated(fake_two_stream_reader, fake_writer) -> None:
    call_count = 0
    completed = 0

    async def app_with_one_error(conn, _receive, send):
        nonlocal call_count, completed
        call_count += 1
        if conn.path == '/' and call_count == 1:
            raise RuntimeError('stream error')
        await send({'type': 'http.response.start', 'status': 200, 'headers': []})
        await send({'type': 'http.response.body', 'body': b''})
        completed += 1

    aggregator = AsyncMock(spec=EventAggregator)
    actor = HTTP2Actor(fake_two_stream_reader, fake_writer, app_with_one_error, aggregator)
    await actor.run()

    aggregator.on_error.assert_called_once()
    assert call_count == 2 and completed == 1  # sibling stream unaffected


# ---------------------------------------------------------------------------
# Test 4: conn.client and conn.server populated from TCP metadata
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_conn_has_client_and_server(fake_h2_reader, fake_writer) -> None:
    """Regression: HTTP2Actor must inject peername/sockname into the Connection."""
    received = {}

    async def capture_app(conn, receive, send):
        received['client'] = conn.client
        received['server'] = conn.server
        await send({'type': 'http.response.start', 'status': 200, 'headers': []})
        await send({'type': 'http.response.body', 'body': b''})

    aggregator = AsyncMock(spec=EventAggregator)
    actor = HTTP2Actor(
        fake_h2_reader, fake_writer, capture_app, aggregator,
        peername=('192.168.1.1', 54321),
        sockname=('0.0.0.0', 443),
    )
    await actor.run()

    assert received['client'] == ('192.168.1.1', 54321)
    assert received['server'] == ('0.0.0.0', 443)


# ---------------------------------------------------------------------------
# Test 5: flow-control tuning — handshake and sender initialisation
# ---------------------------------------------------------------------------

def _parse_frames(data: bytes) -> list:
    """Parse a raw byte stream into FrameFactory-loaded frame objects."""
    factory = FrameFactory()
    frames = []
    offset = 0
    mv = memoryview(data)
    while offset + 9 <= len(mv):
        length = int.from_bytes(mv[offset:offset + 3], 'big')
        if offset + 9 + length > len(mv):
            break
        frames.append(factory.load(bytes(mv[offset:offset + 9 + length])))
        offset += 9 + length
    return frames


@pytest.mark.asyncio
async def test_handshake_sends_settings_initial_window_size(
        fake_h2_reader, mock_app, monkeypatch):
    """HTTP2Actor.run() must advertise the configured
    ``BB_H2_INITIAL_WINDOW_SIZE`` in the server's SETTINGS frame.

    Sprint 37 lowered BlackBull's default to the RFC 7540 §6.9.2
    baseline (65535).  This test sets a non-default value via env so
    the emitted frame is unambiguously distinguishable from the RFC
    default and the test asserts the actor honours the configured
    value rather than asserting "larger than the default" (which
    would tautologically pass on the old default and fail on the new
    one without exercising the actual code path).
    """
    monkeypatch.setenv('BB_H2_INITIAL_WINDOW_SIZE', '1048576')
    from blackbull.env import reset_settings_cache
    reset_settings_cache()

    writer = _FakeWriter()
    aggregator = AsyncMock(spec=EventAggregator)
    actor = HTTP2Actor(fake_h2_reader, writer, mock_app, aggregator)
    await actor.run()

    settings_frames = [f for f in _parse_frames(bytes(writer.written))
                       if f.FrameType() == FrameTypes.SETTINGS
                       and getattr(f, 'initial_window_size', None) is not None]
    assert settings_frames, 'Server must send SETTINGS with INITIAL_WINDOW_SIZE'
    assert settings_frames[0].initial_window_size == 1048576
    reset_settings_cache()


@pytest.mark.asyncio
async def test_handshake_sends_connection_window_update(
        fake_h2_reader, mock_app, monkeypatch):
    """HTTP2Actor.run() must send WINDOW_UPDATE(stream_id=0) when the
    configured connection window exceeds the RFC 7540 default (65535).

    Sprint 37 lowered BlackBull's default to 65535 (no
    WINDOW_UPDATE needed at handshake — peer's connection window
    already starts there).  This test sets a non-default to assert
    the actor emits the expansion frame when one is required.
    """
    monkeypatch.setenv('BB_H2_CONNECTION_WINDOW_SIZE', '4194304')
    from blackbull.env import reset_settings_cache
    reset_settings_cache()

    writer = _FakeWriter()
    aggregator = AsyncMock(spec=EventAggregator)
    actor = HTTP2Actor(fake_h2_reader, writer, mock_app, aggregator)
    await actor.run()

    conn_wus = [f for f in _parse_frames(bytes(writer.written))
                if f.FrameType() == FrameTypes.WINDOW_UPDATE and f.stream_id == 0]
    assert conn_wus, 'Server must send a connection-level WINDOW_UPDATE at startup'
    assert conn_wus[0].window_size > 0
    reset_settings_cache()


@pytest.mark.asyncio
async def test_make_sender_uses_peer_initial_window_size(fake_writer, mock_app):
    """Stream senders created after the peer's SETTINGS must use the peer's announced IWS."""
    peer_iws = 2097152  # 2 MiB — larger than the RFC default
    aggregator = AsyncMock(spec=EventAggregator)
    actor = HTTP2Actor(None, fake_writer, mock_app, aggregator)
    actor._peer_initial_window_size = peer_iws

    sender = actor.make_sender(stream_id=5)

    assert sender.stream_window_size == peer_iws, (
        f'Expected stream window {peer_iws}, got {sender.stream_window_size}'
    )


@pytest.mark.asyncio
async def test_make_sender_uses_current_connection_window(fake_writer, mock_app):
    """Stream senders created after a connection WINDOW_UPDATE must see the updated budget."""
    aggregator = AsyncMock(spec=EventAggregator)
    actor = HTTP2Actor(None, fake_writer, mock_app, aggregator)
    actor._conn_window.size = 4194304  # simulates post-WINDOW_UPDATE value

    sender = actor.make_sender(stream_id=7)

    # The sender shares the actor's connection window (bug 1.2), so it reads
    # the current budget from the one object rather than a stale copy.
    assert sender.connection_window_size == 4194304, (
        f'Expected connection window 4194304, got {sender.connection_window_size}'
    )
    assert sender._conn_window is actor._conn_window, (
        'stream senders must share the actor connection window, not copy it'
    )


# ---------------------------------------------------------------------------
# Test 6: max_concurrent_streams — SETTINGS advertising and limit enforcement
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_handshake_includes_max_concurrent_streams(fake_h2_reader, mock_app):
    """Server's initial SETTINGS must advertise SETTINGS_MAX_CONCURRENT_STREAMS."""
    writer = _FakeWriter()
    aggregator = AsyncMock(spec=EventAggregator)
    actor = HTTP2Actor(fake_h2_reader, writer, mock_app, aggregator)
    await actor.run()

    settings_frames = [f for f in _parse_frames(bytes(writer.written))
                       if f.FrameType() == FrameTypes.SETTINGS
                       and getattr(f, 'max_concurrent_streams', None) is not None]
    assert settings_frames, 'Server must send SETTINGS with MAX_CONCURRENT_STREAMS'
    assert settings_frames[0].max_concurrent_streams > 0


@pytest.mark.asyncio
async def test_max_concurrent_streams_sends_rst_on_overflow(fake_writer, mock_app):
    """When the active stream count equals the limit, new HEADERS must receive RST_STREAM."""
    aggregator = AsyncMock(spec=EventAggregator)
    headers_frame = _make_headers_frame(stream_id=1, end_stream=True)
    reader = _FakeReader(headers_frame)
    actor = HTTP2Actor(reader, fake_writer, mock_app, aggregator)

    # Fill the limit to exactly capacity before the request arrives
    actor.max_concurrent_streams = 0  # immediate refusal

    await actor.run()

    rst_frames = [f for f in _parse_frames(bytes(fake_writer.written))
                  if f.FrameType() == FrameTypes.RST_STREAM]
    assert rst_frames, 'RST_STREAM must be sent when the concurrent-stream limit is exceeded'


@pytest.mark.asyncio
async def test_max_concurrent_streams_does_not_dispatch_app(fake_writer):
    """App must NOT be called for streams refused by the concurrent-stream limit."""
    called = []

    async def counting_app(scope, receive, send):
        called.append(scope.get('path'))
        await send({'type': 'http.response.start', 'status': 200, 'headers': []})
        await send({'type': 'http.response.body', 'body': b''})

    aggregator = AsyncMock(spec=EventAggregator)
    headers_frame = _make_headers_frame(stream_id=1, end_stream=True)
    reader = _FakeReader(headers_frame)
    actor = HTTP2Actor(reader, fake_writer, counting_app, aggregator)
    actor.max_concurrent_streams = 0  # refuse all streams

    await actor.run()

    assert called == [], 'App must not be dispatched for refused streams'


@pytest.mark.asyncio
async def test_active_stream_count_zero_after_completion(fake_h2_reader, fake_writer, mock_app):
    """_active_stream_count must return to zero once the connection closes."""
    aggregator = AsyncMock(spec=EventAggregator)
    actor = HTTP2Actor(fake_h2_reader, fake_writer, mock_app, aggregator)
    await actor.run()

    assert actor._active_stream_count == 0, (
        f'Expected _active_stream_count=0 after all streams finished, '
        f'got {actor._active_stream_count}'
    )


# ---------------------------------------------------------------------------
# Test 7: per-connection active-streams semaphore (#12 multiplex fairness)
# ---------------------------------------------------------------------------

def test_stream_semaphore_active_by_default_single_worker():
    """Single-worker default: semaphore is created with BB_H2_ACTIVE_STREAMS_1W=20."""
    writer = _FakeWriter()

    async def noop_app(scope, receive, send):
        pass

    # BB_WORKERS defaults to 1, BB_H2_ACTIVE_STREAMS_1W defaults to 20
    actor = HTTP2Actor(None, writer, noop_app, None)
    assert actor._stream_semaphore is not None, (
        '_stream_semaphore should be active in single-worker mode by default'
    )
    assert actor._stream_semaphore._value == 20


def test_stream_semaphore_active_by_default_multi_worker(monkeypatch):
    """Multi-worker default: semaphore active with BB_H2_ACTIVE_STREAMS=20."""
    monkeypatch.setenv('BB_WORKERS', '4')
    from blackbull import env as _env
    monkeypatch.setattr(_env, '_cached_settings', None, raising=False)

    writer = _FakeWriter()

    async def noop_app(scope, receive, send):
        pass

    actor = HTTP2Actor(None, writer, noop_app, None)
    assert actor._stream_semaphore is not None, (
        '_stream_semaphore should be active for multi-worker with default BB_H2_ACTIVE_STREAMS=20'
    )
    assert actor._stream_semaphore._value == 20


def test_stream_semaphore_disabled_when_explicitly_zero(monkeypatch):
    """BB_H2_ACTIVE_STREAMS=0 disables the semaphore regardless of worker count."""
    monkeypatch.setenv('BB_WORKERS', '4')
    monkeypatch.setenv('BB_H2_ACTIVE_STREAMS', '0')
    from blackbull import env as _env
    monkeypatch.setattr(_env, '_cached_settings', None, raising=False)

    writer = _FakeWriter()

    async def noop_app(scope, receive, send):
        pass

    actor = HTTP2Actor(None, writer, noop_app, None)
    assert actor._stream_semaphore is None, (
        '_stream_semaphore should be None when BB_H2_ACTIVE_STREAMS=0'
    )


@pytest.mark.asyncio
async def test_stream_semaphore_caps_active_handlers():
    """When _stream_semaphore is set, no more than LIMIT handlers run concurrently."""
    LIMIT = 2
    STREAM_COUNT = 5

    peak = 0
    active = 0
    gate = asyncio.Event()

    async def gated_app(scope, receive, send):
        nonlocal active, peak
        active += 1
        if active > peak:
            peak = active
        await gate.wait()
        active -= 1
        await send({'type': 'http.response.start', 'status': 200, 'headers': []})
        await send({'type': 'http.response.body', 'body': b''})

    frames = b''.join(_make_headers_frame(2 * i + 1) for i in range(STREAM_COUNT))
    reader = _FakeReader(frames)
    writer = _FakeWriter()
    actor = HTTP2Actor(reader, writer, gated_app, None)
    actor._stream_semaphore = asyncio.Semaphore(LIMIT)

    run_task = asyncio.create_task(actor.run())
    await asyncio.sleep(0.05)  # let frame loop spawn all tasks; semaphore gates them

    assert peak <= LIMIT, f'peak={peak} exceeded semaphore limit {LIMIT}'
    assert active > 0, 'at least one handler should be running while gate is closed'

    gate.set()
    await asyncio.wait_for(run_task, timeout=2.0)
    assert active == 0, 'all handlers should have finished after gate opened'


# ---------------------------------------------------------------------------
# Alloc-reduction (P2 / Sprint 80): the H2 HEADERS dispatch path skips the
# per-request AccessLogRecord (and the capturing-send wrapper) when nothing
# consumes it — mirrors the HTTP/1.1 gate (P3). A real EventAggregator is
# required so has_request_completed_listeners() reports true registration
# state (an AsyncMock returns a truthy mock and would always build the record).
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_h2_baseline_hot_path_skips_access_log_record():
    """Access logging off + no request_completed listener ⇒ the H2 actor must
    not build the AccessLogRecord (nor the conn.state['access_log'] it forces)."""
    import logging
    from blackbull import BlackBull

    app = BlackBull()
    seen: dict = {}

    @app.route(path='/')
    async def index(conn):
        seen['state_keys'] = list(conn.state.keys())
        return b'ok'

    agg = EventAggregator(app._dispatcher)
    reader = _FakeReader(_make_headers_frame(stream_id=1, end_stream=True))
    actor = HTTP2Actor(reader, _FakeWriter(), app, agg)
    access_logger = logging.getLogger('blackbull.access')
    prev = access_logger.level
    access_logger.setLevel(logging.WARNING)  # ensure INFO disabled
    try:
        await actor.run()
    finally:
        access_logger.setLevel(prev)

    assert 'access_log' not in seen['state_keys']


@pytest.mark.asyncio
async def test_h2_request_completed_listener_forces_access_log_record():
    """The inverse: a request_completed listener reads the record's wire fields,
    so the H2 actor must still build it and publish it on conn.state."""
    from blackbull import BlackBull

    app = BlackBull()
    seen: dict = {}

    @app.route(path='/')
    async def index(conn):
        seen['state_keys'] = list(conn.state.keys())
        return b'ok'

    @app.on('request_completed')
    async def _rc(event):
        pass

    agg = EventAggregator(app._dispatcher)
    reader = _FakeReader(_make_headers_frame(stream_id=1, end_stream=True))
    actor = HTTP2Actor(reader, _FakeWriter(), app, agg)
    await actor.run()

    assert 'access_log' in seen['state_keys']
