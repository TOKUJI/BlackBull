"""Integration tests for TrustedProxy — X-Forwarded-* header rewriting."""
import asyncio
from multiprocessing import Process

import httpx
import pytest

from blackbull import BlackBull, JSONResponse
from blackbull.middleware.proxy import TrustedProxy
from .conftest import live_server


def _make_app_trusted() -> BlackBull:
    """App that trusts 127.0.0.1 (the loopback default)."""
    app = BlackBull()
    app.use(TrustedProxy(['127.0.0.1', '::1']))

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
    app.use(TrustedProxy(['10.0.0.1']))

    @app.route(path='/client-ip')
    async def client_ip(scope):
        return {'ip': (scope.get('client') or [''])[0]}

    return app


@pytest.fixture(scope="module")
def live_trusted():
    app = _make_app_trusted()
    with live_server(app) as handle:
        yield handle
@pytest.fixture(scope="module")
def live_untrusted():
    app = _make_app_untrusted()
    with live_server(app) as handle:
        yield handle
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
