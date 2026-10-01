"""Handlers observe only proxy metadata vouched for by the trusted suffix."""
import httpx
import pytest

from blackbull import BlackBull
from blackbull.env import reset_settings_cache
from blackbull.testing import NativeTestServer

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


@pytest.mark.parametrize('lane', ['native-wire', 'scope-wire', 'external-asgi'])
@pytest.mark.parametrize('headers,trusted,expected', [
    pytest.param([
        ('X-Forwarded-For', '198.51.100.99, 203.0.113.10, 10.0.0.2'),
        ('X-Forwarded-Proto', 'https'), ('X-Forwarded-Prefix', '/api/'),
    ], ['127.0.0.1', '10.0.0.0/8'],
        ('203.0.113.10', 'https', '/api'), id='xff-spoof-prefix'),
    pytest.param([
        ('Forwarded', 'for=198.51.100.99;proto=https'),
        ('Forwarded', 'for=203.0.113.10;proto=http, for=10.0.0.2;proto=https'),
        ('X-Forwarded-Proto', 'https'), ('X-Forwarded-Prefix', '/api/'),
    ], ['127.0.0.1', '10.0.0.0/8'],
        ('203.0.113.10', 'http', '/api'), id='forwarded-repeated-fields'),
    pytest.param([
        ('Forwarded', 'for=198.51.100.99;proto=http'),
        ('Forwarded', 'for="[2001:db8::10]:1234";proto=https, for=10.0.0.2'),
    ], ['127.0.0.1', '10.0.0.0/8'],
        ('2001:db8::10', 'https', ''), id='forwarded-ipv6'),
    pytest.param([
        ('Forwarded', 'for=198.51.100.99;proto=https, for=unknown'),
        ('X-Forwarded-For', '198.51.100.99'), ('X-Forwarded-Proto', 'https'),
    ], ['127.0.0.1'], ('127.0.0.1', 'http', ''), id='unknown-barrier'),
    pytest.param([
        ('X-Forwarded-For', '203.0.113.10'),
        ('X-Forwarded-Proto', 'https'), ('X-Forwarded-Proto', 'http'),
        ('X-Forwarded-Prefix', '/forged, /api'),
    ], ['127.0.0.1'], ('203.0.113.10', 'http', ''), id='ambiguous-metadata'),
    pytest.param([
        ('Forwarded', 'for=198.51.100.99;proto=https'),
        ('X-Forwarded-Prefix', '/forged'),
    ], ['10.0.0.0/8'], ('127.0.0.1', 'http', ''), id='untrusted-peer'),
])
async def test_handler_identity_across_dispatch_lanes(monkeypatch, lane, headers, trusted, expected):
    monkeypatch.setenv('BB_FORCE_ASGI_SCOPE', '1' if lane == 'scope-wire' else '0')
    reset_settings_cache()
    app = BlackBull(trusted_proxies=trusted)

    @app.route(path='/identity')
    async def identity(conn):
        return {'ip': conn.client[0], 'scheme': conn.scheme, 'root_path': conn.root_path}

    if lane == 'external-asgi':
        transport = httpx.ASGITransport(app=app, client=('127.0.0.1', 45678))
        async with httpx.AsyncClient(transport=transport, base_url='http://testserver') as client:
            response = await client.get('/identity', headers=headers)
    else:
        async with NativeTestServer(app) as server:
            response = await server.client.get('/identity', headers=headers)

    assert response.status_code == 200, response.text
    assert response.json() == dict(zip(('ip', 'scheme', 'root_path'), expected, strict=True))
