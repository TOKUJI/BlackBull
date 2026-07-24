"""Tests for the request_received Level B event.

Fires after HTTP headers are parsed and the scope is built, before
routing or handler dispatch.  Never fires for WebSocket connections.
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
from blackbull.headers import Headers


# ---------------------------------------------------------------------------
# Shared test infrastructure
# ---------------------------------------------------------------------------

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
# 1. Fires on normal HTTP request
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_request_received_fires():
    """request_received fires once for a normal HTTP GET."""
    app = BlackBull()
    captured: list[Event] = []
    seen = asyncio.Event()

    @app.on('request_received')
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
    assert captured[0].name == 'request_received'


# ---------------------------------------------------------------------------
# 2. Detail shape
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_request_received_detail_shape():
    """detail carries scope, client_ip, method, path, http_version, headers."""
    app = BlackBull()
    captured: list[Event] = []
    seen = asyncio.Event()

    @app.on('request_received')
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
    assert isinstance(d['http_version'], str)
    assert hasattr(d['headers'], '__iter__')


# ---------------------------------------------------------------------------
# 3. Fires before handler executes
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_request_received_fires_before_handler():
    """request_received (via intercept) runs before the route handler."""
    app = BlackBull()
    order: list[str] = []

    @app.intercept('request_received')
    async def interceptor(event: Event):
        order.append('event')

    @app.route(path='/order')
    async def handler(scope, receive, send):
        order.append('handler')
        await send({'type': 'http.response.start', 'status': 200, 'headers': []})
        await send({'type': 'http.response.body', 'body': b'', 'more_body': False})

    await _run_request(app, _raw_request(path='/order'))
    # Interceptors run synchronously inside emit, so no wait needed.
    assert order == ['event', 'handler'], (
        f'Expected [event, handler], got {order}'
    )


# ---------------------------------------------------------------------------
# 4. Exactly-once per request
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_request_received_exactly_once():
    """A single request produces exactly one request_received event."""
    app = BlackBull()
    count = 0
    seen = asyncio.Event()

    @app.on('request_received')
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
# 5. Fires per HTTP/2 stream
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_request_received_fires_per_http2_stream():
    """On HTTP/2, request_received fires once per stream (one dispatch each).

    Sprint 64 moved emission from the server layer into BlackBull._dispatch,
    so per-stream firing is now per-app-call firing.
    """
    app = BlackBull()
    captured: list[Event] = []
    done = asyncio.Event()

    @app.on('request_received')
    async def observer(event: Event) -> None:
        captured.append(event)
        if len(captured) >= 2:
            done.set()

    @app.route(path='/stream{n}')
    async def handler(scope, receive, send):
        await send({'type': 'http.response.start', 'status': 200, 'headers': []})
        await send({'type': 'http.response.body', 'body': b'', 'more_body': False})

    async def noop_send(event, *args, **kwargs):
        pass

    async def fake_receive():
        return {'type': 'http.request', 'body': b'', 'more_body': False}

    def _make_h2_scope(path: str) -> dict:
        return {
            'type': 'http',
            'method': 'GET',
            'path': path,
            'http_version': '2',
            'client': ['10.0.0.1', 0],
            'headers': Headers([]),
            'state': {},
        }

    scope1 = _make_h2_scope('/stream1')
    scope2 = _make_h2_scope('/stream2')

    await asyncio.gather(
        app(scope1, fake_receive, noop_send),
        app(scope2, fake_receive, noop_send),
    )

    await asyncio.wait_for(done.wait(), timeout=2.0)
    await asyncio.sleep(0.2)
    assert len(captured) == 2
    paths = {e.detail['path'] for e in captured}
    assert paths == {'/stream1', '/stream2'}


# ---------------------------------------------------------------------------
# 6. Not fired for WebSocket
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_request_received_not_fired_for_websocket():
    """A WebSocket upgrade must not fire request_received."""
    raw = _ws_request()
    app = BlackBull()
    fired: list[Event] = []

    @app.on('request_received')
    async def observer(event: Event):
        fired.append(event)

    with patch.object(WebSocketActor, 'run', new=AsyncMock()):
        actor = HTTP1Actor(
            _FakeReader(raw), _FakeWriter(), app, None,
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
async def test_request_received_exactly_once_with_aggregator():
    """Exactly one request_received per request under the production actor path.

    Sprint 64 moved emission from the actor layer into BlackBull._dispatch;
    this pins the no-double-fire property when an EventAggregator is wired
    (the production server configuration).
    """
    from blackbull.event_aggregator import EventAggregator

    app = BlackBull()
    count = 0
    seen = asyncio.Event()

    @app.on('request_received')
    async def observer(event: Event):
        nonlocal count
        count += 1
        seen.set()

    @app.route(path='/once-agg')
    async def handler(scope, receive, send):
        await send({'type': 'http.response.start', 'status': 200, 'headers': []})
        await send({'type': 'http.response.body', 'body': b'', 'more_body': False})

    writer = _FakeWriter()
    raw = _raw_request(path='/once-agg')
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
