"""Tests for the CORS middleware."""
import pytest

from blackbull.middleware.cors import CORS
from blackbull.server.headers import Headers


def _make_scope(method='GET', origin=None, headers=None, type_='http'):
    raw = list((headers or {}).items())
    if origin:
        raw.append((b'origin', origin.encode()))
    return {
        'type': type_,
        'method': method,
        'headers': Headers(raw),
    }


async def _call(mw, scope):
    """Run middleware; return (sent_events, call_next_called)."""
    sent = []
    called = []

    async def send(event):
        sent.append(event)

    async def call_next(s, r, se):
        called.append(True)
        # simulate a minimal response through the wrapped send
        await se({'type': 'http.response.start', 'status': 200, 'headers': []})
        await se({'type': 'http.response.body', 'body': b'ok', 'more_body': False})

    await mw(scope, None, send, call_next)
    return sent, called


def _header(events, name: bytes) -> bytes | None:
    for event in events:
        if event.get('type') == 'http.response.start':
            for k, v in event.get('headers', []):
                if k.lower() == name:
                    return v
    return None


# ---------------------------------------------------------------------------
# Pass-through cases
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_no_origin_passes_through():
    mw = CORS(allow_origins=['https://example.com'])
    scope = _make_scope(origin=None)
    sent, called = await _call(mw, scope)
    assert called == [True]
    assert _header(sent, b'access-control-allow-origin') is None


@pytest.mark.asyncio
async def test_disallowed_origin_passes_through():
    mw = CORS(allow_origins=['https://example.com'])
    scope = _make_scope(origin='https://evil.com')
    sent, called = await _call(mw, scope)
    assert called == [True]
    assert _header(sent, b'access-control-allow-origin') is None


@pytest.mark.asyncio
async def test_websocket_scope_passes_through():
    mw = CORS(allow_origins=['*'])
    scope = _make_scope(origin='https://example.com', type_='websocket')
    called = []

    async def call_next(s, r, se):
        called.append(True)

    await mw(scope, None, None, call_next)
    assert called == [True]


# ---------------------------------------------------------------------------
# Actual cross-origin requests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_explicit_origin_adds_acao_and_vary():
    mw = CORS(allow_origins=['https://example.com'])
    scope = _make_scope(origin='https://example.com')
    sent, called = await _call(mw, scope)
    assert called == [True]
    assert _header(sent, b'access-control-allow-origin') == b'https://example.com'
    assert _header(sent, b'vary') == b'Origin'


@pytest.mark.asyncio
async def test_wildcard_origin_adds_star_no_vary():
    mw = CORS(allow_origins=['*'])
    scope = _make_scope(origin='https://any.com')
    sent, _ = await _call(mw, scope)
    assert _header(sent, b'access-control-allow-origin') == b'*'
    assert _header(sent, b'vary') is None


@pytest.mark.asyncio
async def test_allow_credentials_adds_header():
    mw = CORS(allow_origins=['https://example.com'], allow_credentials=True)
    scope = _make_scope(origin='https://example.com')
    sent, _ = await _call(mw, scope)
    assert _header(sent, b'access-control-allow-credentials') == b'true'
    assert _header(sent, b'vary') == b'Origin'


@pytest.mark.asyncio
async def test_expose_headers_present():
    mw = CORS(allow_origins=['*'], expose_headers=['X-Request-Id', 'X-RateLimit-Limit'])
    scope = _make_scope(origin='https://any.com')
    sent, _ = await _call(mw, scope)
    val = _header(sent, b'access-control-expose-headers')
    assert val is not None
    assert b'X-Request-Id' in val


# ---------------------------------------------------------------------------
# Preflight
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_preflight_short_circuits():
    mw = CORS(allow_origins=['https://example.com'])
    scope = _make_scope(
        method='OPTIONS',
        origin='https://example.com',
        headers={b'access-control-request-method': b'POST'},
    )
    sent, called = await _call(mw, scope)
    assert called == []   # call_next never called
    assert sent[0]['status'] == 200
    assert _header(sent, b'access-control-allow-methods') is not None
    assert _header(sent, b'access-control-allow-headers') is not None
    assert _header(sent, b'access-control-max-age') == b'600'


@pytest.mark.asyncio
async def test_preflight_max_age_none_omits_header():
    mw = CORS(allow_origins=['https://example.com'], max_age=None)
    scope = _make_scope(
        method='OPTIONS',
        origin='https://example.com',
        headers={b'access-control-request-method': b'GET'},
    )
    sent, _ = await _call(mw, scope)
    assert _header(sent, b'access-control-max-age') is None


@pytest.mark.asyncio
async def test_preflight_custom_max_age():
    mw = CORS(allow_origins=['*'], max_age=3600)
    scope = _make_scope(
        method='OPTIONS',
        origin='https://any.com',
        headers={b'access-control-request-method': b'DELETE'},
    )
    sent, _ = await _call(mw, scope)
    assert _header(sent, b'access-control-max-age') == b'3600'


# ---------------------------------------------------------------------------
# Constructor validation
# ---------------------------------------------------------------------------

def test_credentials_with_wildcard_raises():
    with pytest.raises(ValueError, match='allow_credentials'):
        CORS(allow_origins=['*'], allow_credentials=True)
