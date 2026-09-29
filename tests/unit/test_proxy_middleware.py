"""Tests for TrustedProxy."""
import pytest

from blackbull.middleware.proxy import TrustedProxy
from blackbull.headers import Headers


def _make_scope(client_ip, headers: dict[bytes, bytes], type_='http'):
    raw = [(k, v) for k, v in headers.items()]
    return {
        'type': type_,
        'client': [client_ip, 12345],
        'scheme': 'http',
        'headers': Headers(raw),
    }


async def _call(mw, scope):
    called = []

    async def call_next(s, r, se):
        called.append(s)

    await mw(scope, None, None, call_next)
    return scope, called


# ---------------------------------------------------------------------------
# X-Forwarded-For
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize('proxy,peer,xff,expected', [
    pytest.param('127.0.0.1', '127.0.0.1', b'203.0.113.5', ['203.0.113.5', 0],
                 id='trusted-xff'),
    pytest.param('127.0.0.1', '1.2.3.4', b'203.0.113.5', ['1.2.3.4', 12345],
                 id='untrusted-peer'),
    pytest.param('10.0.0.0/8', '10.42.0.1', b'203.0.113.7', ['203.0.113.7', 0],
                 id='cidr-range-trusted'),
    pytest.param('10.0.0.0/8', '192.168.1.1', b'203.0.113.7', ['192.168.1.1', 12345],
                 id='cidr-range-outside'),
])
async def test_trusted_xff_updates_client(proxy, peer, xff, expected):
    mw = TrustedProxy(proxy)
    scope = _make_scope(peer, {b'x-forwarded-for': xff})
    scope, _ = await _call(mw, scope)
    assert scope['client'] == expected


@pytest.mark.asyncio
async def test_xff_chain_skips_trusted_hops():
    """A trusted intermediate proxy permits traversal to its observed peer."""
    mw = TrustedProxy(['127.0.0.1', '10.0.0.1'])
    scope = _make_scope('127.0.0.1', {b'x-forwarded-for': b'203.0.113.5, 10.0.0.1'})
    scope, _ = await _call(mw, scope)
    assert scope['client'] == ['203.0.113.5', 0]


# ---------------------------------------------------------------------------
# X-Forwarded-Proto
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize('peer,expected', [
    pytest.param('127.0.0.1', 'https', id='trusted-xfp'),
    pytest.param('9.9.9.9', 'http', id='untrusted-xfp'),
])
async def test_trusted_xfp_updates_scheme(peer, expected):
    mw = TrustedProxy('127.0.0.1')
    scope = _make_scope(peer, {b'x-forwarded-proto': b'https'})
    scope, _ = await _call(mw, scope)
    assert scope['scheme'] == expected


# ---------------------------------------------------------------------------
# CIDR notation
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# RFC 7239 Forwarded header
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_forwarded_header_precedence():
    """RFC 7239 Forwarded wins over X-Forwarded-* when both present."""
    mw = TrustedProxy('127.0.0.1')
    scope = _make_scope('127.0.0.1', {
        b'forwarded':        b'for=203.0.113.9;proto=https',
        b'x-forwarded-for':  b'1.1.1.1',
        b'x-forwarded-proto': b'http',
    })
    scope, _ = await _call(mw, scope)
    assert scope['client'] == ['203.0.113.9', 0]
    assert scope['scheme'] == 'https'


@pytest.mark.asyncio
async def test_forwarded_header_for_only():
    mw = TrustedProxy('127.0.0.1')
    scope = _make_scope('127.0.0.1', {b'forwarded': b'for=203.0.113.1'})
    scope, _ = await _call(mw, scope)
    assert scope['client'] == ['203.0.113.1', 0]
    assert scope['scheme'] == 'http'   # unchanged — no proto directive


@pytest.mark.asyncio
async def test_forwarded_multi_element_stops_at_untrusted_hop():
    mw = TrustedProxy('127.0.0.1')
    scope = _make_scope('127.0.0.1', {
        b'forwarded': b'for=203.0.113.1;proto=https, for=198.51.100.17',
    })
    scope, _ = await _call(mw, scope)
    assert scope['client'] == ['198.51.100.17', 0]
    assert scope['scheme'] == 'http'


@pytest.mark.asyncio
async def test_forwarded_multi_element_no_proto_leak():
    """Only the selected element supplies the scheme."""
    mw = TrustedProxy('127.0.0.1')
    scope = _make_scope('127.0.0.1', {
        b'forwarded': b'for=203.0.113.1, for=198.51.100.17;proto=https',
    })
    scope, _ = await _call(mw, scope)
    assert scope['client'] == ['198.51.100.17', 0]
    assert scope['scheme'] == 'https'


# ---------------------------------------------------------------------------
# WebSocket and non-HTTP scopes
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_websocket_scope_updated():
    mw = TrustedProxy('127.0.0.1')
    scope = _make_scope('127.0.0.1', {b'x-forwarded-for': b'203.0.113.3'}, type_='websocket')
    scope, _ = await _call(mw, scope)
    assert scope['client'] == ['203.0.113.3', 0]


@pytest.mark.asyncio
async def test_non_http_scope_passthrough():
    mw = TrustedProxy('127.0.0.1')
    scope = {'type': 'lifespan'}
    called = []

    async def call_next(s, r, se):
        called.append(True)

    await mw(scope, None, None, call_next)
    assert called == [True]
    assert 'client' not in scope
