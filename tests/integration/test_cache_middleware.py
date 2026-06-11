"""Integration test for the response-cache middleware.

Exercises ``Cache`` end-to-end through the full app stack with a
handler that increments a counter on every invocation.  Two requests
to the same path must hit the handler only once (cache HIT on the
second).  Adds an ``If-None-Match`` round-trip against the auto-
generated ETag.
"""
import pytest

from blackbull import BlackBull
from blackbull.middleware.cache import Cache
from blackbull.testing import TestClient


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


@pytest.fixture
def client():
    # Per-test rather than per-module: each test wants the cache to start clean.
    with TestClient(_make_app()) as c:
        yield c


@pytest.mark.integration
def test_second_request_served_from_cache(client):
    """Two requests to / must yield the same body — the counter only ticks once."""
    r1 = client.get('/')
    r2 = client.get('/')
    assert r1.text == r2.text, (
        f'cached response should match; got {r1.text!r} vs {r2.text!r}')


@pytest.mark.integration
def test_if_none_match_returns_304(client):
    r1 = client.get('/')
    etag = r1.headers.get('etag')
    assert etag is not None, 'middleware must inject an ETag'

    r2 = client.get('/', headers={'If-None-Match': etag})
    assert r2.status_code == 304, (
        f'matching If-None-Match must return 304; got {r2.status_code}')
    assert r2.content == b''


@pytest.mark.integration
def test_cache_control_private_not_cached(client):
    """A handler emitting ``Cache-Control: private`` must NOT be cached
    by the middleware — the counter increments every request."""
    client.get('/private')
    client.get('/private')
    client.get('/private')
    # The body of /private is constant so we observe via the / counter.
    r = client.get('/')
    assert r.text == '4', (
        f'private responses must not be cached; expected counter=4, got {r.text}')
