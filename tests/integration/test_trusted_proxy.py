"""Integration tests for TrustedProxy — X-Forwarded-* header rewriting."""
import pytest

from blackbull import BlackBull
from blackbull.middleware.proxy import TrustedProxy
from blackbull.testing import TestClient


def _make_app_trusted() -> BlackBull:
    """App that trusts 127.0.0.1 (the loopback default).

    ``httpx.ASGITransport`` sets ``scope['client']`` to ``('127.0.0.1', 123)``
    by default, so 127.0.0.1 in the trust list is what makes TestClient
    requests appear to come from a trusted peer.
    """
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
    """App that trusts only 10.0.0.1 — so 127.0.0.1 (TestClient's default
    peer) is NOT trusted."""
    app = BlackBull()
    app.use(TrustedProxy(['10.0.0.1']))

    @app.route(path='/client-ip')
    async def client_ip(scope):
        return {'ip': (scope.get('client') or [''])[0]}

    return app


@pytest.fixture(scope="module")
def trusted():
    with TestClient(_make_app_trusted()) as c:
        yield c


@pytest.fixture(scope="module")
def untrusted():
    with TestClient(_make_app_untrusted()) as c:
        yield c


@pytest.mark.integration
def test_x_forwarded_for_rewritten(trusted):
    r = trusted.get('/client-ip', headers={'X-Forwarded-For': '203.0.113.1'})
    assert r.status_code == 200
    assert r.json()['ip'] == '203.0.113.1'


@pytest.mark.integration
def test_x_forwarded_proto_rewritten(trusted):
    r = trusted.get('/scheme', headers={'X-Forwarded-Proto': 'https'})
    assert r.status_code == 200
    assert r.json()['scheme'] == 'https'


@pytest.mark.integration
def test_untrusted_sender_header_ignored(untrusted):
    """When the peer is not trusted, X-Forwarded-For must not rewrite scope['client']."""
    r = untrusted.get('/client-ip', headers={'X-Forwarded-For': '203.0.113.99'})
    assert r.status_code == 200
    # The IP should be the actual peer (127.0.0.1), not the forwarded value.
    assert r.json()['ip'] != '203.0.113.99'
