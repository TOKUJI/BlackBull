import pytest
from unittest.mock import AsyncMock
import asyncio

from blackbull.event_aggregator import EventAggregator
from blackbull.protocol.frame import FrameFactory
from blackbull.server.recipient import AbstractReader
from blackbull.server.sender import AbstractWriter
from blackbull.server.http2_actor import HTTP2Actor


class _FakeReader(AbstractReader):
    def __init__(self, data: bytes):
        self._buf = bytearray(data)

    async def readexactly(self, n: int) -> bytes:
        if len(self._buf) < n:
            raise asyncio.IncompleteReadError(bytes(self._buf), n)
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

    async def read(self, n: int) -> bytes:
        chunk = bytes(self._buf[:n])
        del self._buf[:n]
        return chunk


class _FakeWriter(AbstractWriter):
    def __init__(self):
        self.written = bytearray()

    def write(self, data: bytes) -> None:
        self.written += data

    async def drain(self) -> None:
        pass

    def close(self) -> None:
        pass

    async def wait_closed(self) -> None:
        pass


@pytest.fixture
def fake_h2_reader():
    # Simulate a simple HTTP/2 connection with one request stream (stream 1)
    data = b''
    return _FakeReader(data)

@pytest.fixture
def fake_writer():
    return _FakeWriter()

@pytest.fixture
def mock_app():
    async def app(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b""})
    return AsyncMock(side_effect=app)

@pytest.mark.asyncio
async def test_stream_request_lifecycle(fake_h2_reader, fake_writer, mock_app) -> None:
    aggregator = AsyncMock(spec=EventAggregator)
    actor = HTTP2Actor(fake_h2_reader, fake_writer, mock_app, aggregator)
    await actor.run()

    aggregator.on_request_received.assert_called_once()
    aggregator.on_request_completed.assert_called_once()
    aggregator.on_error.assert_not_called()

@pytest.mark.asyncio
async def test_two_concurrent_streams(fake_two_stream_reader, fake_writer, mock_app) -> None:
    aggregator = AsyncMock(spec=EventAggregator)
    actor = HTTP2Actor(fake_two_stream_reader, fake_writer, mock_app, aggregator)
    await actor.run()
    assert aggregator.on_request_completed.call_count == 2

@pytest.mark.asyncio
async def test_stream_error_isolated(fake_two_stream_reader, fake_writer) -> None:
    call_count = 0
    async def app_with_one_error(scope, receive, send):
        nonlocal call_count
        call_count += 1
        if scope["_stream_id"] == 1:
            raise RuntimeError("stream 1 error")
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    aggregator = AsyncMock(spec=EventAggregator)
    actor = HTTP2Actor(fake_two_stream_reader, fake_writer, aggregator=aggregator, app=app_with_one_error)
    await actor.run()

    # Error fired for stream 1
    aggregator.on_error.assert_called_once()
    # Stream 3 completed successfully
    aggregator.on_request_completed.assert_called_once()

@pytest.mark.asyncio
async def test_server_push_stream(fake_push_reader, fake_writer, push_app) -> None:
    aggregator = AsyncMock(spec=EventAggregator)
    actor = HTTP2Actor(fake_push_reader, fake_writer, push_app, aggregator)
    await actor.run()
    # Both the request stream and the pushed stream complete
    assert aggregator.on_request_completed.call_count == 2