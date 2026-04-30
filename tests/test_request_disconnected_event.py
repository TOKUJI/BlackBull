"""Tests for the request_disconnected Level B event.

Affected-tests baseline (must not regress after implementation):
- test_request_completed_event.py::* (request_completed must not fire for
  disconnected requests or WebSocket connections)
- test_server_dispatch.py::TestHTTPDisconnect::* (http.disconnect ASGI event
  still reaches the handler unchanged)
- test_server_dispatch.py::TestAccessLogging::* (access log still fires for
  every request, including disconnected ones)

Discovery note: no pre-existing mid-request-disconnect test harness exists
in the test suite.  _drive_request_with_disconnect() below adapts the
MagicMock pattern from test_server_dispatch.py::TestHTTPDisconnect.
"""
import asyncio
import pytest
from unittest.mock import MagicMock, AsyncMock, patch
from blackbull import BlackBull
from blackbull.event import Event
from blackbull.server.server import HTTP11Handler, WebSocketHandler


# ---------------------------------------------------------------------------
# Shared test infrastructure
# ---------------------------------------------------------------------------

class _FakeTransport:
    def __init__(self, peername=None, sockname=None, ssl_object=None):
        self._extras = {
            'peername': peername,
            'sockname': sockname,
            'ssl_object': ssl_object,
        }

    def get_extra_info(self, key, default=None):
        return self._extras.get(key, default)


class _FakeWriter:
    def __init__(self, peername=('127.0.0.1', 54321), sockname=('0.0.0.0', 8000)):
        self.written = bytearray()
        self.transport = _FakeTransport(peername=peername, sockname=sockname)

    def write(self, data: bytes):
        self.written += data

    async def drain(self):
        pass

    def close(self):
        pass

    async def wait_closed(self):
        pass


class _FakeReader:
    """Replay a pre-built byte string through the asyncio StreamReader API."""

    def __init__(self, data: bytes):
        self._buf = bytearray(data)

    async def readline(self) -> bytes:
        idx = self._buf.find(b'\n')
        if idx == -1:
            chunk, self._buf = bytes(self._buf), bytearray()
            return chunk
        chunk = bytes(self._buf[:idx + 1])
        del self._buf[:idx + 1]
        return chunk

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
        if len(self._buf) < n:
            raise asyncio.IncompleteReadError(bytes(self._buf), n)
        chunk = bytes(self._buf[:n])
        del self._buf[:n]
        return chunk


def _raw_request(method: str = 'GET', path: str = '/',
                 version: str = 'HTTP/1.1', host: str = 'localhost:8000') -> bytes:
    lines = [f'{method} {path} {version}', f'Host: {host}', '', '']
    return '\r\n'.join(lines).encode()


def _ws_request(path: str = '/ws') -> bytes:
    """Build a minimal HTTP/1.1 WebSocket upgrade request."""
    lines = [
        f'GET {path} HTTP/1.1',
        'Host: localhost:8000',
        'Upgrade: websocket',
        'Connection: Upgrade',
        'Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==',
        'Sec-WebSocket-Version: 13',
        '',
        '',
    ]
    return '\r\n'.join(lines).encode()


async def _run_request(app, raw: bytes) -> None:
    """Run a single HTTP/1.1 request through HTTP11Handler and return."""
    writer = _FakeWriter()
    handler = HTTP11Handler(app, _FakeReader(b''), writer, raw[:1])
    handler.request = raw
    await handler.run()


async def _drive_request_with_disconnect(
        app, path: str,
        handler_started: asyncio.Event | None = None) -> None:
    """Drive a GET request where the handler calls receive() twice to detect disconnect.

    HTTP1Recipient for a GET request (no Content-Length):
    - First receive() → {'type': 'http.request', 'body': b'', 'more_body': False}
    - Second receive() → {'type': 'http.disconnect'}  (done flag already set)

    The handler must call receive() at least twice.  The second call triggers
    the request_disconnected emit.  handler_started (if provided) is set by the
    handler before the second receive(), so it is always set by the time this
    coroutine returns.
    """
    raw = _raw_request(path=path)
    writer = _FakeWriter()
    handler = HTTP11Handler(app, _FakeReader(b''), writer, raw[:1])
    handler.request = raw
    await handler.run()


# ---------------------------------------------------------------------------
# Disconnect detection — basic firing
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_disconnect_fires_request_disconnected_event():
    """A handler that polls receive() gets http.disconnect on the second call,
    which fires request_disconnected."""
    app = BlackBull()
    captured: list[Event] = []
    seen = asyncio.Event()
    handler_started = asyncio.Event()

    @app.on('request_disconnected')
    async def observer(event: Event):
        captured.append(event)
        seen.set()

    @app.route(path='/slow')
    async def slow_handler(scope, receive, send):
        handler_started.set()
        await receive()          # http.request (empty body for GET)
        await receive()          # http.disconnect → triggers event

    await _drive_request_with_disconnect(app, '/slow', handler_started)
    await asyncio.wait_for(seen.wait(), timeout=2.0)
    await asyncio.sleep(0.2)
    assert len(captured) == 1
    assert captured[0].name == 'request_disconnected'
    assert captured[0].detail['path'] == '/slow'


# ---------------------------------------------------------------------------
# Mutual exclusion with request_completed
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_disconnected_request_does_not_fire_request_completed():
    """request_completed must NOT fire for a request that fired request_disconnected."""
    app = BlackBull()
    completed: list[Event] = []
    disconnected: list[Event] = []
    disconnect_seen = asyncio.Event()

    @app.on('request_completed')
    async def on_completed(event: Event):
        completed.append(event)

    @app.on('request_disconnected')
    async def on_disconnected(event: Event):
        disconnected.append(event)
        disconnect_seen.set()

    @app.route(path='/slow')
    async def slow_handler(scope, receive, send):
        await receive()   # http.request
        await receive()   # http.disconnect — triggers request_disconnected

    await _drive_request_with_disconnect(app, '/slow')
    await asyncio.wait_for(disconnect_seen.wait(), timeout=2.0)
    await asyncio.sleep(0.3)
    assert len(disconnected) == 1
    assert len(completed) == 0, (
        f"request_completed must not fire for disconnected requests, "
        f"got {len(completed)}"
    )


# ---------------------------------------------------------------------------
# Negative test for request_completed — WebSocket connections
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_websocket_connection_does_not_fire_request_completed():
    """A WebSocket connection must not fire request_completed at any point."""
    raw = _ws_request()
    writer = _FakeWriter()
    app = BlackBull()
    completed: list[Event] = []

    @app.on('request_completed')
    async def on_completed(event: Event):
        completed.append(event)

    with patch.object(WebSocketHandler, 'run', new=AsyncMock()):
        reader = _FakeReader(raw)
        handler = HTTP11Handler(app, reader, writer, raw[:1])
        handler.request = raw
        await handler.run()

    await asyncio.sleep(0.3)
    assert len(completed) == 0, (
        f"request_completed must not fire for WebSocket connections, "
        f"got {len(completed)}"
    )


# ---------------------------------------------------------------------------
# Detail shape
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_disconnected_event_detail_has_request_identification_fields():
    """detail carries scope and flat identification fields.

    Response-side fields (status, response_bytes, duration_ms) are absent —
    they are not meaningful for an aborted request.
    """
    app = BlackBull()
    captured: list[Event] = []
    seen = asyncio.Event()

    @app.on('request_disconnected')
    async def observer(event: Event):
        captured.append(event)
        seen.set()

    @app.route(path='/x')
    async def slow_handler(scope, receive, send):
        await receive()
        await receive()

    await _drive_request_with_disconnect(app, '/x')
    await asyncio.wait_for(seen.wait(), timeout=2.0)
    await asyncio.sleep(0.2)
    assert len(captured) == 1

    d = captured[0].detail
    assert 'scope' in d
    assert d['method'] == 'GET'
    assert d['path'] == '/x'
    assert 'client_ip' in d
    assert 'http_version' in d
    assert d['scope']['method'] == d['method']
    assert d['scope']['path'] == d['path']
    assert 'status' not in d
    assert 'response_bytes' not in d
    assert 'duration_ms' not in d


# ---------------------------------------------------------------------------
# Exactly-once semantics
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_disconnect_fires_request_disconnected_exactly_once():
    """A single disconnect produces exactly one event — no duplicates."""
    app = BlackBull()
    captured: list[Event] = []
    seen = asyncio.Event()

    @app.on('request_disconnected')
    async def observer(event: Event):
        captured.append(event)
        seen.set()

    @app.route(path='/once')
    async def slow_handler(scope, receive, send):
        await receive()
        await receive()

    await _drive_request_with_disconnect(app, '/once')
    await asyncio.wait_for(seen.wait(), timeout=2.0)
    await asyncio.sleep(0.3)
    assert len(captured) == 1


# ---------------------------------------------------------------------------
# Negative: normal response must not fire request_disconnected
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_normal_response_does_not_fire_request_disconnected():
    """A request that completes normally must not fire request_disconnected."""
    app = BlackBull()
    disconnected: list[Event] = []
    completed: list[Event] = []
    completed_seen = asyncio.Event()

    @app.on('request_disconnected')
    async def on_disconnected(event: Event):
        disconnected.append(event)

    @app.on('request_completed')
    async def on_completed(event: Event):
        completed.append(event)
        completed_seen.set()

    @app.route(path='/ok')
    async def handler(scope, receive, send):
        await send({'type': 'http.response.start', 'status': 200, 'headers': []})
        await send({'type': 'http.response.body', 'body': b'ok', 'more_body': False})

    await _run_request(app, _raw_request(path='/ok'))
    await asyncio.wait_for(completed_seen.wait(), timeout=2.0)
    await asyncio.sleep(0.3)
    assert len(completed) == 1, 'request_completed must fire for normal request'
    assert len(disconnected) == 0, (
        f'request_disconnected must not fire for normal request, '
        f'got {len(disconnected)}'
    )


# ---------------------------------------------------------------------------
# Negative: WebSocket connections must not fire request_disconnected
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_websocket_connection_does_not_fire_request_disconnected():
    """A WebSocket connection must not fire request_disconnected at any point."""
    from unittest.mock import AsyncMock, patch
    raw = _ws_request()
    writer = _FakeWriter()
    app = BlackBull()
    disconnected: list[Event] = []

    @app.on('request_disconnected')
    async def on_disconnected(event: Event):
        disconnected.append(event)

    with patch.object(WebSocketHandler, 'run', new=AsyncMock()):
        reader = _FakeReader(raw)
        handler = HTTP11Handler(app, reader, writer, raw[:1])
        handler.request = raw
        await handler.run()

    await asyncio.sleep(0.3)
    assert len(disconnected) == 0, (
        f'request_disconnected must not fire for WebSocket connections, '
        f'got {len(disconnected)}'
    )
