"""Integration test for the response-cache middleware.

Drives a real BlackBull server with ``Cache`` installed and a
handler that increments a counter on every invocation.  Two requests
to the same path must hit the handler only once (cache HIT on the
second).  Adds an ``If-None-Match`` round-trip against the auto-
generated ETag.
"""
import asyncio
from multiprocessing import Process

import httpx
import pytest
import pytest_asyncio

from blackbull import BlackBull
from blackbull.middleware.cache import Cache
from .conftest import live_server


def _make_app() -> BlackBull:
    app = BlackBull()
    app.use(Cache(max_age=60))

    state = {'count': 0}

    @app.route(path='/')
    async def index(scope, receive, send):
        state['count'] += 1
        body = str(state['count']).encode()
        await send({'type': 'http.response.start', 'status': 200,
                    'headers': [(b'content-type', b'text/plain')]})
        await send({'type': 'http.response.body', 'body': body})

    @app.route(path='/private')
    async def private_view(scope, receive, send):
        state['count'] += 1
        await send({'type': 'http.response.start', 'status': 200,
                    'headers': [(b'content-type', b'text/plain'),
                                (b'cache-control', b'private, max-age=60')]})
        await send({'type': 'http.response.body', 'body': b'private'})

    app.state = state   # expose for tests
    return app


@pytest_asyncio.fixture
async def cache_app():
    app = _make_app()
    with live_server(app) as handle:
        yield handle
@pytest.mark.integration
@pytest.mark.asyncio
async def test_second_request_served_from_cache(cache_app):
    """Two requests to / must yield the same body — the counter must
    have only ticked once on the server side."""
    base = f'http://127.0.0.1:{cache_app.port}'
    async with httpx.AsyncClient() as c:
        r1 = await c.get(f'{base}/')
        r2 = await c.get(f'{base}/')
    assert r1.text == r2.text, (
        f'cached response should match; got {r1.text!r} vs {r2.text!r}')


@pytest.mark.integration
@pytest.mark.asyncio
async def test_if_none_match_returns_304(cache_app):
    base = f'http://127.0.0.1:{cache_app.port}'
    async with httpx.AsyncClient() as c:
        r1 = await c.get(f'{base}/')
        etag = r1.headers.get('etag')
        assert etag is not None, 'middleware must inject an ETag'

        r2 = await c.get(f'{base}/', headers={'If-None-Match': etag})
        assert r2.status_code == 304, (
            f'matching If-None-Match must return 304; got {r2.status_code}')
        assert r2.content == b''


@pytest.mark.integration
@pytest.mark.asyncio
async def test_cache_control_private_not_cached(cache_app):
    """A handler emitting ``Cache-Control: private`` must NOT be cached
    by the middleware — the counter increments every request."""
    base = f'http://127.0.0.1:{cache_app.port}'
    async with httpx.AsyncClient() as c:
        await c.get(f'{base}/private')
        await c.get(f'{base}/private')
        await c.get(f'{base}/private')
        # The body always says 'private' so we can't distinguish.  Use the
        # /private handler's counter side-effect by then asking for / (a
        # different cache key) and inspecting the counter via its response.
        r = await c.get(f'{base}/')
    # We hit /private three times AND / once → counter is 4.  The body of
    # / shows the counter at that moment.
    assert r.text == '4', (
        f'private responses must not be cached; expected counter=4, got {r.text}')
