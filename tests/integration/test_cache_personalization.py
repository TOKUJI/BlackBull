import pytest

from blackbull import BlackBull, Response
from blackbull.client import HTTP1Client, HTTP2Client
from blackbull.env import reset_settings_cache
from blackbull.middleware import Cache
from tests.harness import served


pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

COOKIES = (b'', b'session=alice', b'session=bob', b'', b'session=alice', b'session=bob')
FRESH = (b'guest:1', b'alice:2', b'bob:3', b'guest:4', b'alice:5', b'bob:6')
SHARED = (b'guest:1',) * 6
VARIED = (b'guest:1', b'alice:2', b'bob:3', b'guest:1', b'alice:2', b'bob:3')


@pytest.mark.parametrize('force_asgi', [False, True], ids=['native', 'compat'])
@pytest.mark.parametrize('client_type', [HTTP1Client, HTTP2Client], ids=['h1', 'h2'])
@pytest.mark.parametrize('cache_control,vary_cookie,expected', [
    pytest.param(None, False, SHARED, id='default-replays'),
    pytest.param(b'private', False, FRESH, id='private-excludes'),
    pytest.param(b'no-store', False, FRESH, id='no-store-excludes'),
    pytest.param(b'private, no-store', False, FRESH, id='private-no-store-excludes'),
    pytest.param(None, True, VARIED, id='vary-cookie-still-shares-anonymous'),
])
async def test_cookie_cache_sharing_and_protection(
        monkeypatch, force_asgi, client_type, cache_control, vary_cookie, expected):
    monkeypatch.setenv('BB_FORCE_ASGI_SCOPE', str(int(force_asgi)))
    reset_settings_cache()
    app = BlackBull()
    app.use(Cache(max_age=60))
    rendered = 0

    @app.route(path='/account')
    async def account(conn):
        nonlocal rendered
        rendered += 1
        label = {b'': b'guest', b'session=alice': b'alice',
                 b'session=bob': b'bob'}[conn.headers.get(b'cookie')]
        body = label + b':' + str(rendered).encode()
        headers = [(b'set-cookie', b'session=' + body + b'; HttpOnly')]
        if cache_control is not None:
            headers.append((b'cache-control', cache_control))
        if vary_cookie:
            headers.append((b'vary', b'Cookie'))
        return Response(body, content_type='text/plain', headers=headers)

    async with served(app) as server:
        for cookie, body in zip(COOKIES, expected, strict=True):
            async with client_type('127.0.0.1', server.server.port) as client:
                response = await client.request('GET', '/account',
                    headers=[(b'cookie', cookie)] if cookie else [])
            assert response.status == 200
            assert response.body == body
            assert response.headers.get(b'set-cookie') == b'session=' + body + b'; HttpOnly'
            if cache_control is not None:
                assert response.headers.get(b'cache-control') == cache_control
    assert rendered == len(set(expected))
