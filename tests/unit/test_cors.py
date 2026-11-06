"""Tests for the CORS middleware."""
import pytest

from blackbull.middleware.cors import CORS
from blackbull.headers import Headers


def _make_scope(method='GET', origin=None, headers=None, type_='http'):
    raw = list((headers or {}).items())
    if origin:
        raw.append((b'origin', origin.encode()))
    scope = {
        'type': type_,
        'method': method,
        'headers': Headers(raw),
    }
    # HTTP is dispatched as a native Connection; non-HTTP (websocket)
    # still travels as an ASGI scope dict, exactly as the middleware sees it.
    if type_ == 'http':
        from blackbull.connection import Connection
        return Connection.from_scope(scope)
    return scope


async def _call(mw, scope):
    """Run middleware; return (sent_events, call_next_called)."""
    sent = []
    called = []

    async def send(event):
        # CORS is a native producer for its own preflight reply; these tests
        # assert on the ASGI event shape, so the seam is normalised away here.
        from blackbull.native import NativeResponse
        if isinstance(event, NativeResponse):
            sent.extend(event.to_asgi())
        else:
            sent.append(event)

    async def call_next(s, r, se):
        called.append(True)
        # simulate a minimal response through the wrapped send
        await se({'type': 'http.response.start', 'status': 200, 'headers': []})
        await se({'type': 'http.response.body', 'body': b'ok', 'more_body': False})

    await mw(scope, None, send, call_next)
    return sent, called


def _header(events, name: bytes) -> bytes | None:
    # The middleware is @as_middleware-decorated → its sends are normalised
    # to the H1 native contract; expand NativeResponse to its ASGI events.
    from blackbull.native import NativeResponse
    for event in events:
        if isinstance(event, NativeResponse):
            evs = event.to_asgi()
        else:
            evs = [event]
        for ev in evs:
            if ev.get('type') == 'http.response.start':
                for k, v in ev.get('headers', []):
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


@pytest.mark.asyncio
async def test_empty_expose_headers_omits_field():
    mw = CORS(expose_headers=[''])
    sent, _ = await _call(mw, _make_scope(origin='https://example.com'))
    assert _header(sent, b'access-control-expose-headers') is None


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
@pytest.mark.parametrize('max_age', [0, 3600])
async def test_preflight_custom_max_age(max_age):
    mw = CORS(allow_origins=['*'], max_age=max_age)
    scope = _make_scope(
        method='OPTIONS',
        origin='https://any.com',
        headers={b'access-control-request-method': b'DELETE'},
    )
    sent, _ = await _call(mw, scope)
    assert _header(sent, b'access-control-max-age') == str(max_age).encode()


@pytest.mark.asyncio
@pytest.mark.parametrize('credentials,allowed,requested,expected', [
    (True, '*', b' Authorization, X-Token,authorization ', b'authorization,x-token'),
    (True, ['*'], b'Content-Type', b'content-type'),
    (True, '*', None, None),
    (True, '*', b'', None),
    (True, ['X-Allowed'], b'x-other', b'X-Allowed'),
    (True, [], b'x-other', None),
    (True, [''], b'x-other', None),
    (False, ['X-Allowed'], b'x-other', b'X-Allowed'),
    (False, '*', b'authorization', b'*'),
])
async def test_preflight_allowed_headers(credentials, allowed, requested, expected):
    mw = CORS(allow_origins=['https://example.com'], allow_credentials=credentials,
              allow_headers=allowed)
    headers = {b'access-control-request-method': b'POST'}
    if requested is not None:
        headers[b'access-control-request-headers'] = requested
    sent, called = await _call(mw, _make_scope(
        method='OPTIONS', origin='https://example.com', headers=headers))
    assert called == []
    actual = _header(sent, b'access-control-allow-headers')
    if expected is None:
        assert actual is None
    else:
        assert actual is not None
        assert {name.strip().lower() for name in actual.split(b',')} == {
            name.strip().lower() for name in expected.split(b',')}


@pytest.mark.asyncio
async def test_credentialed_preflight_combines_requested_header_fields():
    mw = CORS(allow_origins=['https://example.com'], allow_credentials=True)
    conn = _make_scope(method='OPTIONS', origin='https://example.com', headers={
        b'access-control-request-method': b'POST',
        b'access-control-request-headers': b'Authorization',
    })
    conn.headers.append(b'access-control-request-headers', b'X-Token, authorization')
    sent, _ = await _call(mw, conn)
    actual = _header(sent, b'access-control-allow-headers')
    assert actual is not None
    assert {name.strip().lower() for name in actual.split(b',')} == {b'authorization', b'x-token'}
    vary = b','.join(v for k, v in sent[0]['headers'] if k == b'vary')
    assert {v.strip().lower() for v in vary.split(b',')} == {
        b'origin', b'access-control-request-headers'}


@pytest.mark.asyncio
@pytest.mark.parametrize('method,headers', [
    ('GET', {b'access-control-request-method': b'POST'}),
    ('POST', {b'access-control-request-method': b'GET'}),
    ('OPTIONS', {}),
])
async def test_non_preflight_requests_reach_handler(method, headers):
    mw = CORS(allow_origins=['https://example.com'], allow_credentials=True)
    sent, called = await _call(mw, _make_scope(
        method=method, origin='https://example.com', headers=headers))
    assert called == [True]
    assert _header(sent, b'access-control-allow-methods') is None
    assert _header(sent, b'vary') == b'Origin'


@pytest.mark.asyncio
@pytest.mark.parametrize('allowed', ['*', ['Authorization', 'X-Token']])
async def test_preflight_response_mutation_does_not_leak_between_requests(allowed):
    mw = CORS(allow_origins=['https://a.example', 'https://b.example'],
              allow_credentials=True, allow_headers=allowed, expose_headers=['X-Response'])

    async def mutate(event):
        event.header.append(b'x-leaked', b'first-response')
        event.header.append(b'vary', b'X-Leaked')

    first = _make_scope(method='OPTIONS', origin='https://a.example', headers={
        b'access-control-request-method': b'GET',
        b'access-control-request-headers': b'Authorization',
    })
    await mw(first, None, mutate, None)
    for origin, requested in (('https://b.example', b'X-Token'), ('https://a.example', b'')):
        sent, called = await _call(mw, _make_scope(method='OPTIONS', origin=origin, headers={
            b'access-control-request-method': b'GET',
            b'access-control-request-headers': requested,
        }))
        assert called == []
        assert _header(sent, b'access-control-allow-origin') == origin.encode()
        assert _header(sent, b'access-control-expose-headers') == b'X-Response'
        assert _header(sent, b'x-leaked') is None
        names = _header(sent, b'access-control-allow-headers')
        assert names == (requested or None if allowed == '*' else b'Authorization,X-Token')
        vary = b','.join(v for k, v in sent[0]['headers'] if k == b'vary')
        expected = {b'origin', b'access-control-request-headers'} if allowed == '*' else {b'origin'}
        assert {v.strip().lower() for v in vary.split(b',')} == expected
    sent, _ = await _call(mw, _make_scope(origin='https://b.example'))
    assert _header(sent, b'vary') == b'Origin'
    assert _header(sent, b'access-control-allow-headers') is None
    assert _header(sent, b'x-leaked') is None


@pytest.mark.asyncio
@pytest.mark.parametrize('requested', [b'X-Token\r\nInjected: yes', b'X-Token\x00'])
async def test_preflight_rejects_unsafe_reflected_header_values(requested):
    mw = CORS(allow_origins=['https://example.com'], allow_credentials=True)
    with pytest.raises(ValueError):
        await _call(mw, _make_scope(method='OPTIONS', origin='https://example.com', headers={
            b'access-control-request-method': b'GET',
            b'access-control-request-headers': requested,
        }))


@pytest.mark.asyncio
@pytest.mark.parametrize('option', ['allow_methods', 'allow_headers', 'expose_headers'])
async def test_cors_rejects_unsafe_configured_header_values(option):
    with pytest.raises(ValueError):
        mw = CORS(allow_origins=['https://example.com'],
                  **{option: ['X-Token\r\nInjected: yes']})
        await _call(mw, _make_scope(method='OPTIONS', origin='https://example.com', headers={
            b'access-control-request-method': b'GET',
        }))


# ---------------------------------------------------------------------------
# Constructor validation
# ---------------------------------------------------------------------------

def test_credentials_with_wildcard_raises():
    with pytest.raises(ValueError, match='allow_credentials'):
        CORS(allow_origins=['*'], allow_credentials=True)
