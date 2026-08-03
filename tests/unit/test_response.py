import json
from http import HTTPStatus

import pytest
from hypothesis import given
from hypothesis import strategies as st

from blackbull import JSONResponse, RedirectResponse, Response, WebSocketResponse
from blackbull.app import _wrap_send_native
from blackbull.native import NativeResponse
from blackbull.response import cookie_header

# ---------------------------------------------------------------------------
# Response
# ---------------------------------------------------------------------------

def test_response_to_native():
    r = Response(b'Hi', status=HTTPStatus.CREATED,
                 headers=[(b'x-a', b'1')])
    n = r.to_native()
    assert isinstance(n, NativeResponse)
    assert n.status == 201
    assert n.header is not None
    assert (b'x-a', b'1') in list(n.header)
    assert n.body == b'Hi'
    assert n.more_body is False
    assert n.trailers is None


def test_json_response_to_native():
    n = JSONResponse({'ok': True}).to_native()
    assert n.body == json.dumps({'ok': True}).encode()
    assert n.content_type == b'application/json'


def test_response_to_native_is_single_object():
    # the native serialiser is one object — the wire-emission equivalent of
    # `to_asgi()` returning a start+body pair (1 send vs 2).
    n = Response(b'Hi').to_native()
    assert len(n.to_asgi()) == 2  # start + body, and back again losslessly


@pytest.mark.asyncio
async def test_wrap_native_send_body_none_falls_back_to_empty():
    """Clean-subagent MAJOR: a spec-violating full-form body event with
    body=None must not hang the native sender (it skips a None body and a
    buffered header would never flush) — it falls back to an empty body."""
    from blackbull.app import _wrap_send_native

    sent = []

    async def raw_send(event):
        sent.append(event)

    send = _wrap_send_native(raw_send)
    await send({'type': 'http.response.start', 'status': 200, 'headers': []})
    await send({'type': 'http.response.body', 'body': None, 'more_body': False})

    assert len(sent) == 2
    n = sent[1]
    assert n.body is not None      # falls back to b'', never None
    assert n.body == b''


def test_response_body_from_bytes():
    assert Response(b'hi').body == b'hi'


def test_response_body_from_str():
    assert Response('hi').body == b'hi'


def test_response_default_status():
    assert Response(b'').status == HTTPStatus.OK


def test_response_custom_status():
    assert Response(b'', status=HTTPStatus.NOT_FOUND).status == HTTPStatus.NOT_FOUND


def test_response_content_type_default():
    assert (b'content-type', b'text/html; charset=utf-8') in Response(b'').headers


def test_response_custom_content_type():
    r = Response(b'', content_type='text/plain')
    assert (b'content-type', b'text/plain') in r.headers


def test_response_extra_headers():
    extra = [(b'x-foo', b'bar')]
    r = Response(b'', headers=extra)
    assert (b'x-foo', b'bar') in r.headers


def test_response_str_headers_coerced_to_bytes():
    # Regression: str-tuple custom headers used to pass through unchanged
    # and crash the sender's b''.join with TypeError.  Response now coerces.
    r = Response(b'<h1>hi</h1>',
                 headers=[('Content-Type', 'text/html; charset=utf-8'),
                          ('X-Foo', 'bar')])
    for k, v in r.headers:
        assert isinstance(k, bytes)
        assert isinstance(v, bytes)
    assert (b'X-Foo', b'bar') in r.headers


def test_response_str_headers_reject_non_ascii():
    # RFC 9110 §5.5: header field values are ASCII.  Surface non-ASCII at
    # construction time rather than emitting obs-text bytes onto the wire.
    with pytest.raises(UnicodeEncodeError):
        Response(b'', headers=[('X-Foo', 'café')])


# ---------------------------------------------------------------------------
# headers=dict  (FastAPI/Starlette/httpx convention)
# ---------------------------------------------------------------------------

def test_response_accepts_dict_headers():
    # Regression: passing a dict used to iterate the dict's *keys* and unpack
    # each key string into (k, v) — silent corruption.  A dict now maps to
    # (name, value) tuples via .items(), coerced to bytes.
    r = Response(b'', headers={'X-Foo': 'bar', 'X-Baz': 'qux'})
    assert (b'X-Foo', b'bar') in r.headers
    assert (b'X-Baz', b'qux') in r.headers
    for k, v in r.headers:
        assert isinstance(k, bytes) and isinstance(v, bytes)


def test_jsonresponse_accepts_dict_headers():
    r = JSONResponse({'ok': True}, headers={'X-Trace': 'abc123'})
    assert (b'content-type', b'application/json') in r.headers
    assert (b'X-Trace', b'abc123') in r.headers


def test_redirectresponse_accepts_dict_headers():
    r = RedirectResponse('/new', headers={b'set-cookie': b'sid=abc'})
    assert (b'location', b'/new') in r.headers
    assert (b'set-cookie', b'sid=abc') in r.headers


def test_response_dict_headers_bytes_keys_and_values():
    r = Response(b'', headers={b'X-A': b'1'})
    assert (b'X-A', b'1') in r.headers


def test_response_rejects_bare_string_headers():
    # A bare string is iterable; the old loop would silently mangle it.
    # Asserted on _normalize_headers directly: under beartype instrumentation
    # the str is rejected earlier still, by Response's ``headers`` annotation
    # (a BeartypeCallHintParamViolation) — so both layers reject it loudly.
    from blackbull.response import _normalize_headers
    with pytest.raises(TypeError):
        _normalize_headers('X-Foo: bar')


def test_response_rejects_non_pair_headers():
    with pytest.raises(TypeError):
        Response(b'', headers=[('only-one-element',)])
    with pytest.raises(TypeError):
        Response(b'', headers=[('a', 'b', 'c')])


def test_response_rejects_non_str_bytes_header_value():
    with pytest.raises(TypeError):
        Response(b'', headers={'X-Count': 5})


# ---------------------------------------------------------------------------
# JSONResponse
# ---------------------------------------------------------------------------

_json_value = st.recursive(
    st.one_of(st.none(), st.booleans(), st.integers(),
              st.floats(allow_nan=False, allow_infinity=False), st.text()),
    lambda ch: st.lists(ch) | st.dictionaries(st.text(), ch),
    max_leaves=10,
)


@given(obj=_json_value)
def test_jsonresponse_body_encodes_any_serializable(obj):
    assert JSONResponse(obj).body == json.dumps(obj).encode()


def test_jsonresponse_content_type():
    assert (b'content-type', b'application/json') in JSONResponse({}).headers


def test_jsonresponse_default_status():
    assert JSONResponse({}).status == HTTPStatus.OK


def test_jsonresponse_custom_status():
    assert JSONResponse({}, status=HTTPStatus.BAD_REQUEST).status == HTTPStatus.BAD_REQUEST


def test_jsonresponse_extra_headers():
    extra = [(b'set-cookie', b'sid=abc')]
    r = JSONResponse({}, headers=extra)
    assert (b'set-cookie', b'sid=abc') in r.headers


# ---------------------------------------------------------------------------
# RedirectResponse
# ---------------------------------------------------------------------------

def test_redirectresponse_default_status_is_302():
    assert RedirectResponse('/new').status == HTTPStatus.FOUND


def test_redirectresponse_custom_status():
    r = RedirectResponse('/perm', status=HTTPStatus.MOVED_PERMANENTLY)
    assert r.status == HTTPStatus.MOVED_PERMANENTLY


def test_redirectresponse_sets_location_header():
    assert (b'location', b'/static/favicon.svg') in \
        RedirectResponse('/static/favicon.svg').headers


def test_redirectresponse_empty_body():
    assert RedirectResponse('/new').body == b''


def test_redirectresponse_merges_extra_headers():
    r = RedirectResponse('/new', headers=[(b'set-cookie', b'sid=abc')])
    assert (b'location', b'/new') in r.headers
    assert (b'set-cookie', b'sid=abc') in r.headers


def test_redirectresponse_rejects_non_ascii_url():
    # RFC 9110 §10.2.2 — Location is a URI-reference (ASCII); callers
    # percent-encode non-ASCII URLs before passing them in.
    with pytest.raises(UnicodeEncodeError):
        RedirectResponse('/café')


@pytest.mark.asyncio
async def test_wrap_send_unpacks_redirectresponse():
    calls = []

    async def raw(event):
        calls.append(event)

    r = RedirectResponse('/login', status=HTTPStatus.SEE_OTHER)
    await _wrap_send_native(raw)(r)
    assert len(calls) == 1
    n = calls[0]
    assert isinstance(n, NativeResponse)
    assert n.status == int(HTTPStatus.SEE_OTHER)
    assert (b'location', b'/login') in list(n.header)
    assert n.body == b''


# ---------------------------------------------------------------------------
# cookie_header
# ---------------------------------------------------------------------------

def test_cookie_header_returns_tuple():
    result = cookie_header('sid', 'abc')
    assert isinstance(result, tuple) and len(result) == 2


def test_cookie_header_name():
    assert cookie_header('sid', 'abc')[0] == b'set-cookie'


def test_cookie_header_value_contains_name_and_value():
    v = cookie_header('session_id', 'xyz')[1]
    assert b'session_id=xyz' in v


def test_cookie_header_httponly_by_default():
    assert b'HttpOnly' in cookie_header('sid', 'abc')[1]


def test_cookie_header_no_httponly():
    assert b'HttpOnly' not in cookie_header('sid', 'abc', http_only=False)[1]


def test_cookie_header_path():
    assert b'Path=/' in cookie_header('sid', 'abc')[1]


# ---------------------------------------------------------------------------
# _wrap_send_native
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_wrap_send_unpacks_response():
    """A Response object becomes a single NativeResponse (one send)."""
    calls = []

    async def raw(event):
        calls.append(event)

    r = Response(b'hi', status=HTTPStatus.CREATED)
    await _wrap_send_native(raw)(r)
    assert len(calls) == 1
    n = calls[0]
    assert isinstance(n, NativeResponse)
    assert n.status == int(HTTPStatus.CREATED)
    assert (b'content-type', b'text/html; charset=utf-8') in list(n.header)
    assert n.body == b'hi'
    assert n.more_body is False


@pytest.mark.asyncio
async def test_wrap_send_unpacks_jsonresponse():
    calls = []

    async def raw(event):
        calls.append(event)

    r = JSONResponse({'ok': True}, status=HTTPStatus.UNAUTHORIZED)
    await _wrap_send_native(raw)(r)
    assert len(calls) == 1
    n = calls[0]
    assert isinstance(n, NativeResponse)
    assert n.status == int(HTTPStatus.UNAUTHORIZED)
    assert (b'content-type', b'application/json') in list(n.header)
    assert n.body == b'{"ok": true}'


@pytest.mark.asyncio
async def test_wrap_native_send_converts_dict_to_native():
    """ASGI event dicts are converted to NativeResponse on the native path."""
    calls = []

    async def raw(event):
        calls.append(event)

    evt = {'type': 'http.response.start', 'status': 200, 'headers': []}
    await _wrap_send_native(raw)(evt)
    n = calls[0]
    assert isinstance(n, NativeResponse)
    assert n.status == 200
    assert n.header is not None


@pytest.mark.asyncio
async def test_wrap_native_send_bytes_to_single_native():
    """Bytes become a single NativeResponse (one send), not a start+body pair."""
    calls = []

    async def raw(event):
        calls.append(event)

    await _wrap_send_native(raw)(b'raw bytes')
    assert len(calls) == 1
    n = calls[0]
    assert isinstance(n, NativeResponse)
    assert n.body == b'raw bytes'


# ---------------------------------------------------------------------------
# WebSocketResponse (unchanged)
# ---------------------------------------------------------------------------

def test_WebSocketResponse_str():
    result = WebSocketResponse('hello')
    assert result == {'type': 'websocket.send', 'text': 'hello'}


def test_WebSocketResponse_bytes():
    result = WebSocketResponse(b'\xde\xad\xbe\xef')
    assert result == {'type': 'websocket.send', 'bytes': b'\xde\xad\xbe\xef'}


@given(obj=st.one_of(st.dictionaries(st.text(), st.integers()),
                     st.lists(st.integers())))
def test_WebSocketResponse_collection_is_json_encoded(obj):
    result = WebSocketResponse(obj)
    assert result == {'type': 'websocket.send', 'text': json.dumps(obj)}


def test_WebSocketResponse_empty_string():
    assert WebSocketResponse('') == {'type': 'websocket.send', 'text': ''}


@given(s=st.text())
def test_WebSocketResponse_string_uses_text_field_verbatim(s):
    """str input must appear in 'text' as-is, not JSON-encoded."""
    result = WebSocketResponse(s)
    assert result.get('type') == 'websocket.send'
    assert result.get('text') == s


@given(payload=st.binary())
def test_WebSocketResponse_bytes_uses_bytes_field(payload):
    result = WebSocketResponse(payload)
    assert result.get('type') == 'websocket.send'
    assert result.get('bytes') == payload
