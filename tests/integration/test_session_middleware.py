"""Integration test for the signed-cookie session middleware.

Drives a real BlackBull server with the session middleware installed and
sends two HTTP/1.1 requests over the same client: one to set a session
value, one to read it back.  Confirms the Set-Cookie / Cookie round-trip
works end-to-end.
"""
import asyncio
from multiprocessing import Process

import httpx
import pytest
import pytest_asyncio

from blackbull import BlackBull
from blackbull.middleware.session import SessionMiddleware


def _make_app() -> BlackBull:
    app = BlackBull()
    app.use(SessionMiddleware(secret=b'test-secret', secure=False))

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


@pytest_asyncio.fixture
async def session_app():
    app = _make_app()
    app.create_server(port=0)
    p = Process(target=lambda: asyncio.run(app.run()))
    p.start()
    app.wait_for_port(timeout=10.0)
    yield app
    app.stop()
    p.terminate()
    p.join(timeout=5)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_round_trip_sets_then_reads(session_app):
    base = f'http://127.0.0.1:{session_app.port}'
    async with httpx.AsyncClient() as c:
        r1 = await c.get(f'{base}/set')
        assert r1.status_code == 200
        # The client follows Set-Cookie automatically via the Cookie jar.
        assert 'session' in (r1.cookies or {})

        r2 = await c.get(f'{base}/get')
        assert r2.status_code == 200
        assert r2.text == 'user=alice count=1'


@pytest.mark.integration
@pytest.mark.asyncio
async def test_session_survives_across_requests(session_app):
    base = f'http://127.0.0.1:{session_app.port}'
    async with httpx.AsyncClient() as c:
        for expected in (1, 2, 3, 4):
            r = await c.get(f'{base}/bump')
            assert r.text == str(expected)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_clear_emits_tombstone(session_app):
    base = f'http://127.0.0.1:{session_app.port}'
    async with httpx.AsyncClient() as c:
        await c.get(f'{base}/set')
        assert 'session' in c.cookies

        r = await c.get(f'{base}/clear')
        assert r.status_code == 200
        # After /clear the server has set Max-Age=0 → httpx's cookie jar
        # interprets that as expiration and drops it.
        assert 'session' not in c.cookies, (
            f'cleared session must not persist; jar={dict(c.cookies)}')


@pytest.mark.integration
@pytest.mark.asyncio
async def test_unmodified_session_does_not_emit_set_cookie(session_app):
    base = f'http://127.0.0.1:{session_app.port}'
    async with httpx.AsyncClient() as c:
        r = await c.get(f'{base}/noop')
        assert r.status_code == 200
        # No Set-Cookie header in the response — the session was read but
        # never modified, so no need to round-trip a new cookie.
        assert 'set-cookie' not in {k.lower() for k in r.headers}, (
            f'unmodified session must not emit Set-Cookie; got headers={dict(r.headers)}')


@pytest.mark.integration
@pytest.mark.asyncio
async def test_tampered_cookie_treated_as_empty(session_app):
    base = f'http://127.0.0.1:{session_app.port}'
    async with httpx.AsyncClient() as c:
        # Set a real cookie first, then mangle the MAC.
        await c.get(f'{base}/set')
        real = c.cookies.get('session')
        assert real is not None
        # Replace the MAC half with zeros.
        payload, _, mac = real.partition('.')
        tampered = payload + '.' + '0' * len(mac)
        # New client with the tampered cookie only.
        async with httpx.AsyncClient(cookies={'session': tampered}) as c2:
            r = await c2.get(f'{base}/get')
            # The middleware silently drops the tampered session →
            # empty dict → handler sees None for both keys.
            assert r.text == 'user=None count=None'
