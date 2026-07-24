"""Tests for the before_handler Level B event.

Fires after routing resolves a handler, immediately before the handler
coroutine is awaited.  HTTP only — WebSocket dispatch does not fire it.
"""
import asyncio
import pytest
from unittest.mock import AsyncMock, patch
from blackbull import BlackBull
from blackbull.event import Event
from blackbull.server.http1_actor import HTTP1Actor
from blackbull.server.websocket_actor import WebSocketActor
from blackbull.server.recipient import AbstractReader
from blackbull.server.sender import AbstractWriter


class _FakeWriter(AbstractWriter):
    def __init__(self):
        self.written = bytearray()

    async def write(self, data: bytes) -> None:
        self.written += data


class _FakeReader(AbstractReader):
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
    actor = HTTP1Actor(
        _FakeReader(b''), writer, app, None,
        request=raw,
        peername=('127.0.0.1', 54321),
        sockname=('0.0.0.0', 8000),
    )
    await actor.run()


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
    assert 'conn' in d
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
    actor = HTTP1Actor(
        _FakeReader(b''), writer, app, None,
        request=raw,
        peername=('127.0.0.1', 54321),
        sockname=('0.0.0.0', 8000),
    )
    await actor.run()

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

    with patch.object(WebSocketActor, 'run', new=AsyncMock()):
        actor = HTTP1Actor(
            _FakeReader(raw), writer, app, None,
            request=raw,
            peername=('127.0.0.1', 54321),
        )
        await actor.run()

    await asyncio.sleep(0.2)
    assert len(fired) == 0


# ---------------------------------------------------------------------------
# 7. Exactly-once on the aggregator (production server) path
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_before_handler_exactly_once_with_aggregator():
    """Exactly one before_handler per request under the production actor path.

    Regression for the Sprint 40 candidate-3 double fire: both the actor
    layer (EventAggregator.on_before_handler) and BlackBull._dispatch used
    to emit before_handler, so a request served by BlackBull's own HTTP/1.1
    server fired it twice.  Sprint 64 consolidated emission into _dispatch.
    """
    from blackbull.event_aggregator import EventAggregator

    app = BlackBull()
    count = 0
    seen = asyncio.Event()

    @app.on('before_handler')
    async def observer(event: Event):
        nonlocal count
        count += 1
        seen.set()

    @app.route(path='/hello')
    async def handler(scope, receive, send):
        await send({'type': 'http.response.start', 'status': 200, 'headers': []})
        await send({'type': 'http.response.body', 'body': b'ok', 'more_body': False})

    writer = _FakeWriter()
    raw = _raw_request(path='/hello')
    actor = HTTP1Actor(
        _FakeReader(b''), writer, app, EventAggregator(app._dispatcher),
        request=raw,
        peername=('127.0.0.1', 54321),
        sockname=('0.0.0.0', 8000),
    )
    await actor.run()

    await asyncio.wait_for(seen.wait(), timeout=2.0)
    await asyncio.sleep(0.2)
    assert count == 1
