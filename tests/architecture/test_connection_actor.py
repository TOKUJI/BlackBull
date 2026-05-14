"""Tests for ConnectionActor (Phase 6 Step 5)."""
import asyncio
import pytest
from unittest.mock import AsyncMock

from blackbull.event_aggregator import EventAggregator
from blackbull.server.recipient import AbstractReader, IncompleteReadError
from blackbull.server.sender import AbstractWriter
from blackbull.server.connection_actor import ConnectionActor


# ---------------------------------------------------------------------------
# In-process fakes
# ---------------------------------------------------------------------------

class _FakeReader(AbstractReader):
    """Reader backed by a bytes buffer; raises IncompleteReadError on EOF."""

    def __init__(self, data: bytes):
        self._buf = bytearray(data)

    async def read(self, n: int) -> bytes:
        chunk = bytes(self._buf[:n])
        del self._buf[:n]
        return chunk

    async def readuntil(self, sep: bytes) -> bytes:
        idx = self._buf.find(sep)
        if idx == -1:
            raise IncompleteReadError()
        chunk = bytes(self._buf[:idx + len(sep)])
        del self._buf[:idx + len(sep)]
        return chunk

    async def readexactly(self, n: int) -> bytes:
        if len(self._buf) < n:
            raise IncompleteReadError()
        chunk = bytes(self._buf[:n])
        del self._buf[:n]
        return chunk


class _ByteByByteReader(AbstractReader):
    """Returns at most 1 byte from read(); readexactly(n) reads all n bytes.

    Simulates TCP fragmentation: read(n) short-reads (returns 1 byte), while
    readexactly(n) correctly accumulates n bytes.  A production path that used
    read(8) for the preface remainder would receive only 1 byte and mismatch the
    expected 8-byte sequence, raising ValueError.
    """

    def __init__(self, data: bytes):
        self._buf = bytearray(data)

    async def read(self, n: int) -> bytes:
        if not self._buf:
            return b''
        chunk = bytes(self._buf[:1])
        del self._buf[:1]
        return chunk

    async def readuntil(self, sep: bytes) -> bytes:
        result = bytearray()
        while True:
            if not self._buf:
                raise IncompleteReadError()
            result.append(self._buf[0])
            del self._buf[:1]
            if bytes(result).endswith(sep):
                return bytes(result)

    async def readexactly(self, n: int) -> bytes:
        if len(self._buf) < n:
            raise IncompleteReadError()
        chunk = bytes(self._buf[:n])
        del self._buf[:n]
        return chunk


class _FakeBadReader(AbstractReader):
    """Reader that immediately raises ValueError on any read."""

    async def read(self, n: int) -> bytes:
        raise ValueError('simulated read error')

    async def readuntil(self, sep: bytes) -> bytes:
        raise ValueError('simulated read error')

    async def readexactly(self, n: int) -> bytes:
        raise ValueError('simulated read error')


class _FakeWriter(AbstractWriter):
    def __init__(self):
        self.written = bytearray()

    async def write(self, data: bytes) -> None:
        self.written += data


# ---------------------------------------------------------------------------
# Wire-format constants
# ---------------------------------------------------------------------------

_HTTP2_PREFACE = b'PRI * HTTP/2.0\r\n\r\nSM\r\n\r\n'
_HTTP1_REQUEST = b'GET / HTTP/1.1\r\nHost: localhost:8000\r\n\r\n'


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def fake_http1_reader():
    return _FakeReader(_HTTP1_REQUEST)


@pytest.fixture
def fake_http2_preface_reader():
    return _FakeReader(_HTTP2_PREFACE)


@pytest.fixture
def fake_bad_reader():
    return _FakeBadReader()


@pytest.fixture
def fake_writer():
    return _FakeWriter()


@pytest.fixture
def mock_app():
    async def app(scope, receive, send):
        await send({'type': 'http.response.start', 'status': 200, 'headers': []})
        await send({'type': 'http.response.body', 'body': b''})
    return AsyncMock(side_effect=app)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_connection_dispatches_http1(
        fake_http1_reader, fake_writer, mock_app) -> None:
    aggregator = AsyncMock(spec_set=EventAggregator)
    actor = ConnectionActor(fake_http1_reader, fake_writer, mock_app, aggregator)
    await actor.run()

    aggregator.on_connection_accepted.assert_called_once()
    aggregator.on_request_completed.assert_called_once()


@pytest.mark.asyncio
async def test_connection_dispatches_http2(
        fake_http2_preface_reader, fake_writer, mock_app) -> None:
    aggregator = AsyncMock(spec_set=EventAggregator)
    actor = ConnectionActor(fake_http2_preface_reader, fake_writer, mock_app, aggregator)
    await actor.run()

    aggregator.on_connection_accepted.assert_called_once()


@pytest.mark.asyncio
async def test_connection_isolates_protocol_error(
        fake_bad_reader, fake_writer, mock_app) -> None:
    aggregator = AsyncMock(spec_set=EventAggregator)
    actor = ConnectionActor(fake_bad_reader, fake_writer, mock_app, aggregator)
    await actor.run()  # must not raise

    aggregator.on_error.assert_called_once()
    exc_arg = aggregator.on_error.call_args[0][1]
    assert isinstance(exc_arg, BaseException)


@pytest.mark.asyncio
async def test_connection_dispatches_http2_fragmented_preface(
        fake_writer, mock_app) -> None:
    """ConnectionActor must choose HTTP2Actor even when the preface arrives byte-by-byte.

    Regression: the old _dispatch() used read(8) for the 8-byte preface remainder
    (b'\\r\\nSM\\r\\n\\r\\n').  read(8) on a byte-by-byte reader returns only 1 byte,
    causing the preface validation to fail with ValueError('Invalid HTTP/2 preface
    continuation').  readexactly(8) reads all 8 bytes and the dispatch succeeds.
    """
    reader = _ByteByByteReader(_HTTP2_PREFACE)
    aggregator = AsyncMock(spec_set=EventAggregator)
    actor = ConnectionActor(reader, fake_writer, mock_app, aggregator)
    await actor.run()  # must not raise ValueError

    aggregator.on_connection_accepted.assert_called_once()
