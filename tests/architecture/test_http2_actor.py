"""Tests for HTTP2Actor and StreamActor (Phase 6 Step 4)."""
import asyncio
import pytest
from unittest.mock import AsyncMock
from hpack import Encoder

from blackbull.event_aggregator import EventAggregator
from blackbull.protocol.frame import FrameFactory, FrameTypes, HeaderFrameFlags
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
                             (b':scheme', b'https')])
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
# Helper: aggregator that calls through on_before_handler
# ---------------------------------------------------------------------------

def _call_through_aggregator():
    aggregator = AsyncMock(spec=EventAggregator)

    async def _before(scope, receive, send, *, call_next):
        await call_next(scope, receive, send)

    aggregator.on_before_handler = AsyncMock(side_effect=_before)
    return aggregator


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
# Test 1: single stream → all four lifecycle events fire
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_stream_request_lifecycle(fake_h2_reader, fake_writer, mock_app) -> None:
    aggregator = AsyncMock(spec=EventAggregator)
    actor = HTTP2Actor(fake_h2_reader, fake_writer, mock_app, aggregator)
    await actor.run()

    aggregator.on_request_received.assert_called_once()
    aggregator.on_request_completed.assert_called_once()
    aggregator.on_error.assert_not_called()


# ---------------------------------------------------------------------------
# Test 2: two concurrent streams both complete
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_two_concurrent_streams(fake_two_stream_reader, fake_writer, mock_app) -> None:
    aggregator = AsyncMock(spec=EventAggregator)
    actor = HTTP2Actor(fake_two_stream_reader, fake_writer, mock_app, aggregator)
    await actor.run()
    assert aggregator.on_request_completed.call_count == 2


# ---------------------------------------------------------------------------
# Test 3: stream error → RST_STREAM sent, other stream completes
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_stream_error_isolated(fake_two_stream_reader, fake_writer) -> None:
    call_count = 0

    async def app_with_one_error(scope, _receive, send):
        nonlocal call_count
        call_count += 1
        if scope.get('path') == '/' and call_count == 1:
            raise RuntimeError('stream error')
        await send({'type': 'http.response.start', 'status': 200, 'headers': []})
        await send({'type': 'http.response.body', 'body': b''})

    aggregator = _call_through_aggregator()
    actor = HTTP2Actor(fake_two_stream_reader, fake_writer, app_with_one_error, aggregator)
    await actor.run()

    aggregator.on_error.assert_called_once()
    aggregator.on_request_completed.assert_called_once()
