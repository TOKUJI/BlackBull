"""Tests for HTTP1Actor and RequestActor (Phase 6 Step 3)."""
import asyncio
import pytest
from unittest.mock import ANY, AsyncMock

from blackbull.event_aggregator import EventAggregator
from blackbull.server.http1_actor import HTTP1Actor, RequestActor
from blackbull.server.recipient import AbstractReader, IncompleteReadError
from blackbull.server.sender import AbstractWriter


# ---------------------------------------------------------------------------
# In-process reader/writer fakes (no live sockets)
# ---------------------------------------------------------------------------

class _FakeTransport:
    def __init__(self, peername=('127.0.0.1', 54321), sockname=('0.0.0.0', 8000)):
        self._extras = {
            'peername': peername,
            'sockname': sockname,
            'ssl_object': None,
        }

    def get_extra_info(self, key, default=None):
        return self._extras.get(key, default)


class _FakeWriter(AbstractWriter):
    def __init__(self):
        self.written = bytearray()

    async def write(self, data: bytes) -> None:
        self.written += data


class _FakeReader(AbstractReader):
    """AbstractReader backed by a byte buffer; raises IncompleteReadError on
    empty reads (signals EOF to the handler keep-alive loop)."""

    def __init__(self, data: bytes):
        self._buf = bytearray(data)

    async def read(self, n: int) -> bytes:
        chunk = bytes(self._buf[:n])
        del self._buf[:n]
        return chunk

    async def readuntil(self, sep: bytes) -> bytes:
        idx = self._buf.find(sep)
        if idx == -1:
            # EOF sentinel: return just the separator (keep-alive break signal)
            chunk, self._buf = bytes(self._buf), bytearray()
            return chunk + sep
        chunk = bytes(self._buf[:idx + len(sep)])
        del self._buf[:idx + len(sep)]
        return chunk

    async def readexactly(self, n: int) -> bytes:
        if len(self._buf) < n:
            raise IncompleteReadError(bytes(self._buf))
        chunk = bytes(self._buf[:n])
        del self._buf[:n]
        return chunk


def _http_get(path: str = '/', host: str = 'localhost:8000') -> bytes:
    return f'GET {path} HTTP/1.1\r\nHost: {host}\r\n\r\n'.encode()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_writer():
    return _FakeWriter()


@pytest.fixture
def mock_reader():
    """Single GET request, then EOF (keep-alive terminates cleanly)."""
    return _FakeReader(_http_get())


@pytest.fixture
def mock_keep_alive_reader():
    """Two GET requests back-to-back, then EOF."""
    return _FakeReader(_http_get() + _http_get())


@pytest.fixture
def mock_app():
    return AsyncMock()


# ---------------------------------------------------------------------------
# Helper: aggregator mock that calls through on_before_handler
# ---------------------------------------------------------------------------

def _call_through_aggregator():
    """AsyncMock(spec=EventAggregator) whose on_before_handler calls call_next."""
    aggregator = AsyncMock(spec=EventAggregator)

    async def _before(scope, receive, send, *, call_next):
        await call_next(scope, receive, send)

    aggregator.on_before_handler = AsyncMock(side_effect=_before)
    return aggregator


# ---------------------------------------------------------------------------
# Test 1: single request → all four lifecycle events fire in order
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_request_lifecycle_events(mock_reader, mock_writer, mock_app) -> None:
    aggregator = AsyncMock(spec=EventAggregator)
    actor = HTTP1Actor(mock_reader, mock_writer, mock_app, aggregator)
    await actor.run()

    aggregator.on_request_received.assert_called_once()
    aggregator.on_before_handler.assert_called_once()
    aggregator.on_after_handler.assert_called_once_with(ANY, exception=None)
    aggregator.on_request_completed.assert_called_once()
    aggregator.on_error.assert_not_called()


# ---------------------------------------------------------------------------
# Test 2: app exception → error fires, after_handler carries exception,
#         request_completed does NOT fire
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_app_exception_fires_error(mock_reader, mock_writer) -> None:
    boom = RuntimeError("app error")

    async def bad_app(scope, receive, send):
        raise boom

    # Use a call-through aggregator so bad_app is actually invoked via call_next
    aggregator = _call_through_aggregator()
    actor = HTTP1Actor(mock_reader, mock_writer, bad_app, aggregator)
    await actor.run()  # isolate strategy: exception is swallowed after re-emitting

    aggregator.on_error.assert_called_once_with(ANY, boom)
    call_args = aggregator.on_after_handler.call_args
    assert call_args.kwargs["exception"] is boom
    aggregator.on_request_completed.assert_not_called()


# ---------------------------------------------------------------------------
# Test 3: keep-alive — two sequential requests on the same connection
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_keep_alive_two_requests(mock_keep_alive_reader, mock_writer, mock_app) -> None:
    aggregator = AsyncMock(spec=EventAggregator)
    actor = HTTP1Actor(mock_keep_alive_reader, mock_writer, mock_app, aggregator)
    await actor.run()
    assert aggregator.on_request_completed.call_count == 2


# ---------------------------------------------------------------------------
# Test 4: request_disconnected fires when aggregator reports it
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_request_disconnected_on_eof(mock_reader, mock_writer) -> None:
    """Aggregator.on_request_disconnected fires when the app polls receive() and
    gets back http.disconnect (HTTP1Recipient returns it on the second call once
    the body has been consumed)."""
    disconnected_called = False

    async def app_that_detects_disconnect(scope, receive, send):
        await receive()  # consumes the body (http.request)
        await receive()  # HTTP1Recipient returns http.disconnect on second call

    aggregator = _call_through_aggregator()

    async def _on_disconnect(*args, **kwargs):
        nonlocal disconnected_called
        disconnected_called = True

    aggregator.on_request_disconnected = AsyncMock(side_effect=_on_disconnect)

    actor = HTTP1Actor(mock_reader, mock_writer, app_that_detects_disconnect, aggregator)
    await actor.run()
    assert disconnected_called
