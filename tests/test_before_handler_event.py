"""Tests for the before_handler Level B event.

Fires after routing resolves a handler, immediately before the handler
coroutine is awaited.  HTTP only — WebSocket dispatch does not fire it.
"""
import asyncio
import pytest
from unittest.mock import AsyncMock, patch
from blackbull import BlackBull
from blackbull.event import Event
from blackbull.server.server import HTTP11Handler, WebSocketHandler


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

    def write(self, data: bytes) -> None:
        self.written += data

    async def drain(self) -> None:
        pass

    def close(self) -> None:
        pass

    async def wait_closed(self) -> None:
        pass


class _FakeReader:
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
    lines = [
        f'GET {path} HTTP/1.1',
        'Host: localhost:8000',
        'Upgrade: websocket',
        'Connection: Upgrade',
        'Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==',
        'Sec-WebSocket-Version: 13',
        '', '',
    ]
    return '\r\n'.join(lines).encode()


async def _run_request(app, raw: bytes) -> None:
    writer = _FakeWriter()
    handler = HTTP11Handler(app, _FakeReader(b''), writer, raw[:1])
    handler.request = raw
    await handler.run()


# ---------------------------------------------------------------------------
# 1. Fires on normal request
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_before_handler_fires():
    """before_handler fires once for a normal HTTP GET."""
    app = BlackBull()
    captured: list[Event] = []
    seen = asyncio.Event()

    @app.on('before_handler')
    async def observer(event: Event):
        captured.append(event)
        seen.set()

    @app.route(path='/hello')
    async def handler(scope, receive, send):
        await send({'type': 'http.response.start', 'status': 200, 'headers': []})
        await send({'type': 'http.response.body', 'body': b'ok', 'more_body': False})

    await _run_request(app, _raw_request(path='/hello'))
    await asyncio.wait_for(seen.wait(), timeout=2.0)
    await asyncio.sleep(0.2)
    assert len(captured) == 1
    assert captured[0].name == 'before_handler'


# ---------------------------------------------------------------------------
# 2. Detail shape
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_before_handler_detail_shape():
    """detail carries scope, client_ip, method, path, handler."""
    app = BlackBull()
    captured: list[Event] = []
    seen = asyncio.Event()

    @app.on('before_handler')
    async def observer(event: Event):
        captured.append(event)
        seen.set()

    @app.route(path='/hello')
    async def handler(scope, receive, send):
        await send({'type': 'http.response.start', 'status': 200, 'headers': []})
        await send({'type': 'http.response.body', 'body': b'ok', 'more_body': False})

    await _run_request(app, _raw_request(method='GET', path='/hello'))
    await asyncio.wait_for(seen.wait(), timeout=2.0)
    await asyncio.sleep(0.2)
    assert len(captured) == 1

    d = captured[0].detail
    assert 'scope' in d
    assert isinstance(d['client_ip'], str)
    assert d['method'] == 'GET'
    assert d['path'] == '/hello'
    assert isinstance(d['handler'], str)


# ---------------------------------------------------------------------------
# 3. Ordering: request_received → before_handler → handler_body
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_before_handler_ordering():
    """request_received fires before before_handler, before_handler before handler body."""
    app = BlackBull()
    order: list[str] = []

    @app.intercept('request_received')
    async def rr_interceptor(event: Event):
        order.append('request_received')

    @app.intercept('before_handler')
    async def bh_interceptor(event: Event):
        order.append('before_handler')

    @app.route(path='/order')
    async def handler(scope, receive, send):
        order.append('handler_body')
        await send({'type': 'http.response.start', 'status': 200, 'headers': []})
        await send({'type': 'http.response.body', 'body': b'', 'more_body': False})

    await _run_request(app, _raw_request(path='/order'))
    assert order == ['request_received', 'before_handler', 'handler_body'], (
        f'Expected [request_received, before_handler, handler_body], got {order}'
    )


# ---------------------------------------------------------------------------
# 4. Exactly-once per request
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_before_handler_exactly_once():
    """A single request produces exactly one before_handler event."""
    app = BlackBull()
    count = 0
    seen = asyncio.Event()

    @app.on('before_handler')
    async def observer(event: Event):
        nonlocal count
        count += 1
        seen.set()

    @app.route(path='/once')
    async def handler(scope, receive, send):
        await send({'type': 'http.response.start', 'status': 200, 'headers': []})
        await send({'type': 'http.response.body', 'body': b'', 'more_body': False})

    await _run_request(app, _raw_request(path='/once'))
    await asyncio.wait_for(seen.wait(), timeout=2.0)
    await asyncio.sleep(0.2)
    assert count == 1


# ---------------------------------------------------------------------------
# 5. Interceptor short-circuit
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_before_handler_intercept_short_circuits():
    """An interceptor that raises prevents the route handler from executing."""
    app = BlackBull()
    handler_ran = False

    @app.intercept('before_handler')
    async def guard(event: Event):
        raise PermissionError('blocked')

    @app.route(path='/guarded')
    async def handler(scope, receive, send):
        nonlocal handler_ran
        handler_ran = True
        await send({'type': 'http.response.start', 'status': 200, 'headers': []})
        await send({'type': 'http.response.body', 'body': b'ok', 'more_body': False})

    writer = _FakeWriter()
    raw = _raw_request(path='/guarded')
    h = HTTP11Handler(app, _FakeReader(b''), writer, raw[:1])
    h.request = raw
    await h.run()

    assert not handler_ran
    assert len(writer.written) > 0  # an error response was written


# ---------------------------------------------------------------------------
# 6. Not fired for WebSocket
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_before_handler_not_fired_for_websocket():
    """A WebSocket upgrade must not fire before_handler."""
    raw = _ws_request()
    writer = _FakeWriter()
    app = BlackBull()
    fired: list[Event] = []

    @app.on('before_handler')
    async def observer(event: Event):
        fired.append(event)

    with patch.object(WebSocketHandler, 'run', new=AsyncMock()):
        reader = _FakeReader(raw)
        handler = HTTP11Handler(app, reader, writer, raw[:1])
        handler.request = raw
        await handler.run()

    await asyncio.sleep(0.2)
    assert len(fired) == 0
