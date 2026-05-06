"""Tests for the after_handler Level B event.

Fires immediately after the route handler returns or raises.
Always fires if before_handler fired.  HTTP only.
"""
import asyncio
import pytest
from blackbull import BlackBull
from blackbull.event import Event
from blackbull.server.http1_actor import HTTP1Actor
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


async def _run_request(app, raw: bytes) -> _FakeWriter:
    writer = _FakeWriter()
    actor = HTTP1Actor(
        _FakeReader(b''), writer, app, None,
        request=raw,
        peername=('127.0.0.1', 54321),
        sockname=('0.0.0.0', 8000),
    )
    await actor.run()
    return writer


# ---------------------------------------------------------------------------
# 1. Fires on normal request
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_after_handler_fires():
    """after_handler fires once for a normal HTTP GET."""
    app = BlackBull()
    captured: list[Event] = []
    seen = asyncio.Event()

    @app.on('after_handler')
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
    assert captured[0].name == 'after_handler'


# ---------------------------------------------------------------------------
# 2. Detail has no exception on success
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_after_handler_detail_no_exception():
    """detail['exception'] is None when the handler returns normally."""
    app = BlackBull()
    captured: list[Event] = []
    seen = asyncio.Event()

    @app.on('after_handler')
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

    d = captured[0].detail
    assert 'scope' in d
    assert isinstance(d['client_ip'], str)
    assert d['method'] == 'GET'
    assert d['path'] == '/hello'
    assert isinstance(d['handler'], str)
    assert d['exception'] is None


# ---------------------------------------------------------------------------
# 3. Detail carries the exception when handler raises
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_after_handler_detail_with_exception():
    """detail['exception'] holds the raised exception instance."""
    app = BlackBull()
    captured: list[Event] = []
    seen = asyncio.Event()

    @app.on('after_handler')
    async def observer(event: Event):
        captured.append(event)
        seen.set()

    @app.route(path='/boom')
    async def handler(scope, receive, send):
        raise ValueError('intentional error')

    await _run_request(app, _raw_request(path='/boom'))
    await asyncio.wait_for(seen.wait(), timeout=2.0)
    await asyncio.sleep(0.2)
    assert len(captured) == 1
    assert isinstance(captured[0].detail['exception'], ValueError)


# ---------------------------------------------------------------------------
# 4. Fires even when handler raises
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_after_handler_fires_on_exception():
    """after_handler fires exactly once even if the handler raises."""
    app = BlackBull()
    count = 0
    seen = asyncio.Event()

    @app.on('after_handler')
    async def observer(event: Event):
        nonlocal count
        count += 1
        seen.set()

    @app.route(path='/boom')
    async def handler(scope, receive, send):
        raise RuntimeError('oops')

    await _run_request(app, _raw_request(path='/boom'))
    await asyncio.wait_for(seen.wait(), timeout=2.0)
    await asyncio.sleep(0.2)
    assert count == 1


# ---------------------------------------------------------------------------
# 5. Ordering: handler_body → after_handler → request_completed
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_after_handler_ordering():
    """handler body runs before after_handler, which runs before request_completed."""
    app = BlackBull()
    order: list[str] = []

    @app.intercept('after_handler')
    async def after_interceptor(event: Event):
        order.append('after_handler')

    @app.intercept('request_completed')
    async def completed_interceptor(event: Event):
        order.append('request_completed')

    @app.route(path='/order')
    async def handler(scope, receive, send):
        order.append('handler_body')
        await send({'type': 'http.response.start', 'status': 200, 'headers': []})
        await send({'type': 'http.response.body', 'body': b'', 'more_body': False})

    await _run_request(app, _raw_request(path='/order'))
    assert order == ['handler_body', 'after_handler', 'request_completed'], (
        f'Expected [handler_body, after_handler, request_completed], got {order}'
    )


# ---------------------------------------------------------------------------
# 6. Exactly-once per request
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_after_handler_exactly_once():
    """A single request produces exactly one after_handler event."""
    app = BlackBull()
    count = 0
    seen = asyncio.Event()

    @app.on('after_handler')
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
