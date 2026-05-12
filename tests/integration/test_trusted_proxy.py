"""Integration tests for TrustedProxyMiddleware — X-Forwarded-* header rewriting."""
import asyncio
from multiprocessing import Process

import httpx
import pytest

from blackbull import BlackBull, JSONResponse
from blackbull.middleware.proxy import TrustedProxyMiddleware


def _make_app_trusted() -> BlackBull:
    """App that trusts 127.0.0.1 (the loopback default)."""
    app = BlackBull()
    app.use(TrustedProxyMiddleware(['127.0.0.1', '::1']))

    @app.route(path='/client-ip')
    async def client_ip(scope):
        return {'ip': (scope.get('client') or [''])[0]}

    @app.route(path='/scheme')
    async def scheme(scope):
        return {'scheme': scope.get('scheme', '')}

    return app


def _make_app_untrusted() -> BlackBull:
    """App that trusts only 10.0.0.1 — so 127.0.0.1 is NOT trusted."""
    app = BlackBull()
    app.use(TrustedProxyMiddleware(['10.0.0.1']))

    @app.route(path='/client-ip')
    async def client_ip(scope):
        return {'ip': (scope.get('client') or [''])[0]}

    return app


@pytest.fixture(scope="module")
def live_trusted():
    app = _make_app_trusted()
    app.create_server(port=0)
    p = Process(target=lambda: asyncio.run(app.run()))
    p.start()
    app.wait_for_port(timeout=10.0)
    yield app
    app.stop()
    p.terminate()
    p.join(timeout=5)


@pytest.fixture(scope="module")
def live_untrusted():
    app = _make_app_untrusted()
    app.create_server(port=0)
    p = Process(target=lambda: asyncio.run(app.run()))
    p.start()
    app.wait_for_port(timeout=10.0)
    yield app
    app.stop()
    p.terminate()
    p.join(timeout=5)


def _base(app) -> str:
    return f'http://127.0.0.1:{app.port}'


@pytest.mark.integration
@pytest.mark.asyncio
async def test_x_forwarded_for_rewritten(live_trusted):
    async with httpx.AsyncClient() as c:
        r = await c.get(
            f'{_base(live_trusted)}/client-ip',
            headers={'X-Forwarded-For': '203.0.113.1'},
        )
    assert r.status_code == 200
    assert r.json()['ip'] == '203.0.113.1'


@pytest.mark.integration
@pytest.mark.asyncio
async def test_x_forwarded_proto_rewritten(live_trusted):
    async with httpx.AsyncClient() as c:
        r = await c.get(
            f'{_base(live_trusted)}/scheme',
            headers={'X-Forwarded-Proto': 'https'},
        )
    assert r.status_code == 200
    assert r.json()['scheme'] == 'https'


@pytest.mark.integration
@pytest.mark.asyncio
async def test_untrusted_sender_header_ignored(live_untrusted):
    """When the peer is not trusted, X-Forwarded-For must not rewrite scope['client']."""
    async with httpx.AsyncClient() as c:
        r = await c.get(
            f'{_base(live_untrusted)}/client-ip',
            headers={'X-Forwarded-For': '203.0.113.99'},
        )
    assert r.status_code == 200
    # The IP should be 127.0.0.1 (the actual peer), not the forwarded value
    assert r.json()['ip'] != '203.0.113.99'
