"""Tests for WebSocketActor (Phase 6 Step 5)."""
import asyncio
import pytest
from unittest.mock import AsyncMock

from blackbull.event_aggregator import EventAggregator
from blackbull.server.recipient import AbstractReader
from blackbull.server.sender import AbstractWriter
from blackbull.server.websocket_actor import WebSocketActor


# ---------------------------------------------------------------------------
# Wire-format helpers
# ---------------------------------------------------------------------------

def _ws_masked_frame(payload: bytes, opcode: int = 0x1) -> bytes:
    """Encode a masked WebSocket frame (client → server, RFC 6455 §5.3)."""
    mask_key = b'\x00\x00\x00\x00'
    masked = bytes(b ^ mask_key[i % 4] for i, b in enumerate(payload))
    length = len(payload)
    header = bytes([0x80 | opcode])  # FIN=1
    if length < 126:
        header += bytes([0x80 | length])  # MASK=1
    elif length < 65536:
        header += bytes([0x80 | 126]) + length.to_bytes(2, 'big')
    else:
        header += bytes([0x80 | 127]) + length.to_bytes(8, 'big')
    return header + mask_key + masked


def _ws_unmasked_frame(payload: bytes) -> bytes:
    """Encode an unmasked client frame — protocol violation per RFC 6455 §5.1."""
    return bytes([0x81, len(payload)]) + payload


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
        self.closed = False

    async def write(self, data: bytes) -> None:
        self.written += data

    async def close(self) -> None:
        self.closed = True


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def fake_ws_reader():
    """Empty reader — background task immediately reaches EOF."""
    return _FakeReader(b'')


@pytest.fixture
def fake_ws_reader_two_msgs():
    """Two masked text frames."""
    return _FakeReader(
        _ws_masked_frame(b'hello') +
        _ws_masked_frame(b'world')
    )


@pytest.fixture
def fake_bad_frame_reader():
    """Unmasked client frame — protocol violation."""
    return _FakeReader(_ws_unmasked_frame(b'bad'))


@pytest.fixture
def fake_writer():
    return _FakeWriter()


@pytest.fixture
def mock_ws_app():
    """WebSocket app that receives connect, accepts, then drains until disconnect."""
    async def app(scope, receive, send):
        await receive()  # websocket.connect
        await send({'type': 'websocket.accept'})
        while True:
            event = await receive()
            if event.get('type') == 'websocket.disconnect':
                break
    return app


@pytest.fixture
def mock_ws_app_two_msgs():
    """WebSocket app that calls receive for connect + exactly 2 messages."""
    async def app(scope, receive, send):
        await receive()  # websocket.connect
        await send({'type': 'websocket.accept'})
        await receive()  # message 1
        await receive()  # message 2
    return app


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_websocket_lifecycle_events(
        fake_ws_reader, fake_writer, mock_ws_app) -> None:
    aggregator = AsyncMock(spec_set=EventAggregator)
    scope = {'type': 'websocket', '_connection_id': 'test-id'}
    actor = WebSocketActor(
        fake_ws_reader, fake_writer, scope, mock_ws_app, aggregator)
    await actor.run()

    aggregator.on_websocket_connected.assert_called_once_with(scope, None)
    aggregator.on_websocket_disconnected.assert_called_once_with(scope, code=1006)


@pytest.mark.asyncio
async def test_websocket_message_fires_per_message(
        fake_ws_reader_two_msgs, fake_writer, mock_ws_app_two_msgs) -> None:
    aggregator = AsyncMock(spec_set=EventAggregator)
    scope = {'type': 'websocket', '_connection_id': 'test-id'}
    actor = WebSocketActor(
        fake_ws_reader_two_msgs, fake_writer, scope, mock_ws_app_two_msgs, aggregator)
    await actor.run()

    assert aggregator.on_websocket_message.call_count == 2


@pytest.mark.asyncio
async def test_websocket_protocol_error_isolated(
        fake_bad_frame_reader, fake_writer) -> None:
    from blackbull.server.sender import WebSocketSender
    aggregator = AsyncMock(spec_set=EventAggregator)
    scope = {'type': 'websocket', '_connection_id': 'test-id'}

    async def app(scope, receive, send):
        await receive()  # websocket.connect
        await receive()  # raises ValueError (unmasked frame)

    actor = WebSocketActor(
        fake_bad_frame_reader, fake_writer, scope, app, aggregator)
    await actor.run()  # must not raise

    aggregator.on_error.assert_called_once()
    aggregator.on_websocket_disconnected.assert_called_once_with(scope, code=1006)
    close_1002 = WebSocketSender._encode_frame((1002).to_bytes(2, 'big'), opcode=0x8)
    assert close_1002 in bytes(fake_writer.written), (
        'CLOSE(1002) frame must be written to the wire on protocol violation'
    )


@pytest.mark.asyncio
async def test_protocol_violation_closes_writer(
        fake_bad_frame_reader, fake_writer) -> None:
    """writer.close() must be called explicitly on protocol violations (P1 §7.2)."""
    aggregator = AsyncMock(spec_set=EventAggregator)
    scope = {'type': 'websocket', '_connection_id': 'test-id'}

    async def app(scope, receive, send):
        await receive()   # websocket.connect
        await receive()   # raises ValueError (unmasked frame)

    actor = WebSocketActor(fake_bad_frame_reader, fake_writer, scope, app, aggregator)
    await actor.run()

    assert fake_writer.closed, 'writer.close() must be called after protocol violation'
