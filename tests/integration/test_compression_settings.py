import pytest

from blackbull import BlackBull, Response
from blackbull.client import HTTP1Client, HTTP2Client
from blackbull.env import reset_settings_cache
from blackbull.middleware import Compression
from tests.harness import served


pytestmark = pytest.mark.integration


@pytest.mark.asyncio
@pytest.mark.parametrize('force_asgi', [False, True])
@pytest.mark.parametrize('client_type', [HTTP1Client, HTTP2Client])
@pytest.mark.parametrize('min_size', [1, 1000])
async def test_app_compression_uses_environment(monkeypatch, force_asgi, client_type, min_size):
    brotli = pytest.importorskip('brotli')
    monkeypatch.setenv('BB_FORCE_ASGI_SCOPE', str(int(force_asgi)))
    monkeypatch.setenv('BB_COMPRESSION_MIN_SIZE', str(min_size))
    monkeypatch.setenv('BB_BROTLI_QUALITY', '1')
    reset_settings_cache()
    app = BlackBull()
    app.use(Compression())
    body = b'configured compression ' * 20

    @app.route(path='/data')
    async def data(conn):
        return Response(body, content_type='text/plain')

    async with served(app) as server:
        async with client_type('127.0.0.1', server.server.port) as client:
            for _ in range(2):
                response = await client.request('GET', '/data',
                                               headers=[(b'accept-encoding', b'br')])
                assert response.status == 200
                if min_size == 1:
                    assert response.headers.get(b'content-encoding') == b'br'
                    assert response.body == brotli.compress(body, quality=1)
                else:
                    assert b'content-encoding' not in response.headers
                    assert response.body == body
