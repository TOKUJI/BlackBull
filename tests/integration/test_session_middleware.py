"""Integration test for the signed-cookie session middleware.

Drives the full app stack through TestClient — sends sequenced requests
sharing httpx's cookie jar to confirm the Set-Cookie / Cookie round-trip
works end-to-end, including session clear, tamper rejection, and the
no-Set-Cookie-when-unmodified optimisation.
"""
import pytest

from blackbull import BlackBull
from blackbull.middleware.session import Session
from blackbull.testing import TestClient


def _make_app() -> BlackBull:
    app = BlackBull()
    app.use(Session(secret=b'test-secret', secure=False))

    @app.route(path='/set')
    async def set_view(scope, receive, send):
        scope['session']['user'] = 'alice'
        scope['session']['count'] = 1
        await send({'type': 'http.response.start', 'status': 200,
                    'headers': [(b'content-type', b'text/plain')]})
        await send({'type': 'http.response.body', 'body': b'set'})

    @app.route(path='/get')
    async def get_view(scope, receive, send):
        body = (
            f'user={scope["session"].get("user")} '
            f'count={scope["session"].get("count")}'
        ).encode()
        await send({'type': 'http.response.start', 'status': 200,
                    'headers': [(b'content-type', b'text/plain')]})
        await send({'type': 'http.response.body', 'body': body})

    @app.route(path='/bump')
    async def bump_view(scope, receive, send):
        scope['session']['count'] = scope['session'].get('count', 0) + 1
        await send({'type': 'http.response.start', 'status': 200,
                    'headers': [(b'content-type', b'text/plain')]})
        await send({'type': 'http.response.body',
                    'body': str(scope['session']['count']).encode()})

    @app.route(path='/clear')
    async def clear_view(scope, receive, send):
        scope['session'].clear()
        await send({'type': 'http.response.start', 'status': 200,
                    'headers': [(b'content-type', b'text/plain')]})
        await send({'type': 'http.response.body', 'body': b'cleared'})

    @app.route(path='/noop')
    async def noop_view(scope, receive, send):
        # Reads but doesn't modify the session.
        _ = scope['session'].get('anything')
        await send({'type': 'http.response.start', 'status': 200,
                    'headers': [(b'content-type', b'text/plain')]})
        await send({'type': 'http.response.body', 'body': b'noop'})

    return app


@pytest.fixture
def client():
    # Per-test so each starts with a fresh cookie jar.
    with TestClient(_make_app()) as c:
        yield c


@pytest.mark.integration
def test_round_trip_sets_then_reads(client):
    r1 = client.get('/set')
    assert r1.status_code == 200
    # httpx.Client persists the Set-Cookie in its jar automatically.
    assert 'session' in client.cookies

    r2 = client.get('/get')
    assert r2.status_code == 200
    assert r2.text == 'user=alice count=1'


@pytest.mark.integration
def test_session_survives_across_requests(client):
    for expected in (1, 2, 3, 4):
        r = client.get('/bump')
        assert r.text == str(expected)


@pytest.mark.integration
def test_clear_emits_tombstone(client):
    client.get('/set')
    assert 'session' in client.cookies

    r = client.get('/clear')
    assert r.status_code == 200
    # After /clear the server sets Max-Age=0 → httpx's cookie jar
    # interprets that as expiration and drops it.
    assert 'session' not in client.cookies, (
        f'cleared session must not persist; jar={dict(client.cookies)}')


@pytest.mark.integration
def test_unmodified_session_does_not_emit_set_cookie(client):
    r = client.get('/noop')
    assert r.status_code == 200
    # No Set-Cookie header in the response — the session was read but
    # never modified, so no need to round-trip a new cookie.
    assert 'set-cookie' not in {k.lower() for k in r.headers}, (
        f'unmodified session must not emit Set-Cookie; got headers={dict(r.headers)}')


@pytest.mark.integration
def test_tampered_cookie_treated_as_empty():
    # New client so the tampered cookie is the only one in the jar.
    with TestClient(_make_app()) as c1:
        c1.get('/set')
        real = c1.cookies.get('session')
        assert real is not None
        payload, _, mac = real.partition('.')
        tampered = payload + '.' + '0' * len(mac)

    with TestClient(_make_app(), cookies={'session': tampered}) as c2:
        r = c2.get('/get')
        # The middleware silently drops the tampered session →
        # empty dict → handler sees None for both keys.
        assert r.text == 'user=None count=None'
