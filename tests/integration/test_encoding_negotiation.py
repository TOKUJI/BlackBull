"""Dynamic and precompressed responses respect the same encoding refusals."""
import gzip
from contextlib import AsyncExitStack

import httpx
import pytest
import pytest_asyncio

from blackbull import BlackBull
from blackbull.env import reset_settings_cache
from blackbull.middleware.compression import Compression
from blackbull.testing import NativeTestServer

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]
BODY = b'negotiated response body\n' * 32


@pytest_asyncio.fixture(params=['native-wire', 'scope-wire', 'external-asgi'])
async def client(request, monkeypatch, tmp_path, cache):
    lane = request.param
    monkeypatch.setenv('BB_FORCE_ASGI_SCOPE', '1' if lane == 'scope-wire' else '0')
    reset_settings_cache()
    (tmp_path / 'item.txt').write_bytes(BODY)
    (tmp_path / 'item.txt.gz').write_bytes(gzip.compress(BODY))
    app = BlackBull()
    compression = Compression(min_size=1)

    @app.route(path='/dynamic', middlewares=[compression])
    async def dynamic(conn):
        return BODY.decode()

    @app.route(path='/stream', middlewares=[compression])
    async def stream(conn, receive, send):
        await send({'type': 'http.response.start', 'status': 200,
                    'headers': [(b'content-type', b'text/plain')]})
        await send({'type': 'http.response.body', 'body': BODY[:100], 'more_body': True})
        await send({'type': 'http.response.body', 'body': BODY[100:], 'more_body': False})

    app.static('/static', str(tmp_path), cache=cache)
    async with AsyncExitStack() as stack:
        if lane == 'external-asgi':
            transport = httpx.ASGITransport(app=app)
            active = await stack.enter_async_context(httpx.AsyncClient(
                transport=transport, base_url='http://testserver'))
        else:
            server = await stack.enter_async_context(NativeTestServer(app))
            active = server.client
        yield active
    reset_settings_cache()


@pytest.mark.parametrize('cache', [False, True])
@pytest.mark.parametrize('fields,expected', [
    (['gzip'], 'gzip'),
    (['gzip;q=0, identity;q=1'], None),
    (['*;q=1, gzip;q=0, br;q=0, zstd;q=0'], None),
    (['*;q=1, br;q=0, zstd;q=0'], 'gzip'),
    (['gzip', 'gzip;q=0'], None),
    (['gzip;q=0', 'gzip'], None),
    (['GZIP;Q=0'], None),
    (['gzip;q=bogus,*;q=1,br;q=0,zstd;q=0'], None),
])
async def test_same_negotiation_over_each_dispatch_lane(client, fields, expected):
    headers = [('Accept-Encoding', value) for value in fields]
    for _ in range(2):
        for path in ('/dynamic', '/static/item.txt'):
            async with client.stream('GET', path, headers=headers) as response:
                raw = b''.join([chunk async for chunk in response.aiter_raw()])
                assert response.status_code == 200
                assert response.headers.get('content-encoding') == expected
                assert (gzip.decompress(raw) if expected == 'gzip' else raw) == BODY
                if 'content-length' in response.headers:
                    assert int(response.headers['content-length']) == len(raw)
                if path == '/dynamic' or expected:
                    assert 'accept-encoding' in response.headers['vary'].lower()


@pytest.mark.parametrize('cache', [False])
@pytest.mark.parametrize('accept', ['gzip', 'gzip;q=0'])
async def test_streaming_fallback_preserves_body_and_vary(client, accept):
    async with client.stream('GET', '/stream', headers={'Accept-Encoding': accept}) as response:
        raw = b''.join([chunk async for chunk in response.aiter_raw()])
        assert response.status_code == 200
        assert 'content-encoding' not in response.headers
        assert raw == BODY
        assert 'accept-encoding' in response.headers['vary'].lower()
