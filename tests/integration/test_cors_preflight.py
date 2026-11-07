import pytest

from blackbull import BlackBull, CORS
from blackbull.client import HTTP1Client, HTTP2Client
from blackbull.env import reset_settings_cache
from tests.harness import served


pytestmark = pytest.mark.integration


@pytest.mark.asyncio
@pytest.mark.parametrize('force_asgi', [False, True])
@pytest.mark.parametrize('client_type', [HTTP1Client, HTTP2Client])
@pytest.mark.parametrize('allow_headers', ['*', ['authorization', 'x-token']])
async def test_credentialed_preflight_and_actual_request(
        monkeypatch, force_asgi, client_type, allow_headers):
    monkeypatch.setenv('BB_FORCE_ASGI_SCOPE', '1' if force_asgi else '0')
    reset_settings_cache()
    app = BlackBull()
    app.use(CORS(allow_origins=['https://example.com'], allow_credentials=True,
                 allow_headers=allow_headers))

    @app.route(path='/data')
    async def data(conn):
        return 'ok'

    try:
        async with served(app) as server:
            async with client_type('127.0.0.1', server.server.port) as client:
                for requested in ((b'Authorization', b'X-Token, authorization'), (b'x-token',)):
                    reply = await client.request('OPTIONS', '/data', headers=[
                        (b'origin', b'https://example.com'),
                        (b'access-control-request-method', b'GET'),
                    ] + [(b'access-control-request-headers', value) for value in requested])
                    assert reply.status == 200
                    assert reply.body == b''
                    expected = b','.join(requested) if allow_headers == '*' else b'authorization,x-token'
                    actual = reply.headers.get(b'access-control-allow-headers')
                    assert {name.strip().lower() for name in actual.split(b',')} == {
                        name.strip().lower() for name in expected.split(b',')}
                    assert reply.headers.get(b'access-control-allow-credentials') == b'true'
                reply = await client.request('GET', '/data', headers=[
                    (b'origin', b'https://example.com'), (b'cookie', b'session=test'),
                ])
                assert reply.status == 200
                assert reply.body == b'ok'
                assert reply.headers.get(b'access-control-allow-origin') == b'https://example.com'
                assert reply.headers.get(b'access-control-allow-credentials') == b'true'
    finally:
        reset_settings_cache()
