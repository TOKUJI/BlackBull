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
async def test_trusted_xff_updates_client():
    mw = TrustedProxy('127.0.0.1')
    scope = _make_scope('127.0.0.1', {b'x-forwarded-for': b'203.0.113.5'})
    scope, _ = await _call(mw, scope)
    assert scope['client'] == ['203.0.113.5', 0]


@pytest.mark.asyncio
async def test_untrusted_peer_ignored():
    mw = TrustedProxy('127.0.0.1')
    scope = _make_scope('1.2.3.4', {b'x-forwarded-for': b'203.0.113.5'})
    scope, _ = await _call(mw, scope)
    assert scope['client'] == ['1.2.3.4', 12345]   # unchanged


@pytest.mark.asyncio
async def test_xff_chain_skips_trusted_hops():
    """Leftmost non-trusted IP is the real client, not the intermediate proxy."""
    mw = TrustedProxy(['127.0.0.1', '10.0.0.1'])
    scope = _make_scope('127.0.0.1', {b'x-forwarded-for': b'203.0.113.5, 10.0.0.1'})
    scope, _ = await _call(mw, scope)
    assert scope['client'] == ['203.0.113.5', 0]


# ---------------------------------------------------------------------------
# X-Forwarded-Proto
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_trusted_xfp_updates_scheme():
    mw = TrustedProxy('127.0.0.1')
    scope = _make_scope('127.0.0.1', {b'x-forwarded-proto': b'https'})
    scope, _ = await _call(mw, scope)
    assert scope['scheme'] == 'https'


@pytest.mark.asyncio
async def test_untrusted_xfp_ignored():
    mw = TrustedProxy('127.0.0.1')
    scope = _make_scope('9.9.9.9', {b'x-forwarded-proto': b'https'})
    scope, _ = await _call(mw, scope)
    assert scope['scheme'] == 'http'


# ---------------------------------------------------------------------------
# CIDR notation
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_cidr_range_trusted():
    mw = TrustedProxy('10.0.0.0/8')
    scope = _make_scope('10.42.0.1', {b'x-forwarded-for': b'203.0.113.7'})
    scope, _ = await _call(mw, scope)
    assert scope['client'] == ['203.0.113.7', 0]


@pytest.mark.asyncio
async def test_cidr_range_outside_not_trusted():
    mw = TrustedProxy('10.0.0.0/8')
    scope = _make_scope('192.168.1.1', {b'x-forwarded-for': b'203.0.113.7'})
    scope, _ = await _call(mw, scope)
    assert scope['client'] == ['192.168.1.1', 12345]


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
async def test_forwarded_multi_element_uses_leftmost():
    """RFC 7239 §4 — elements are comma-separated; parse the leftmost only.

    Bug 1.21e: splitting on ';' alone folded the second element's ``for=``
    into the first value, poisoning ``scope['client']``.
    """
    mw = TrustedProxy('127.0.0.1')
    scope = _make_scope('127.0.0.1', {
        b'forwarded': b'for=203.0.113.1;proto=https, for=198.51.100.17',
    })
    scope, _ = await _call(mw, scope)
    assert scope['client'] == ['203.0.113.1', 0]
    assert scope['scheme'] == 'https'


@pytest.mark.asyncio
async def test_forwarded_multi_element_no_proto_leak():
    """A trailing element must not leak its params into the leftmost."""
    mw = TrustedProxy('127.0.0.1')
    scope = _make_scope('127.0.0.1', {
        b'forwarded': b'for=203.0.113.1, for=198.51.100.17;proto=https',
    })
    scope, _ = await _call(mw, scope)
    assert scope['client'] == ['203.0.113.1', 0]
    assert scope['scheme'] == 'http'   # proto belongs to the 2nd element → ignored


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
