import json
from http import HTTPStatus

import pytest
from hypothesis import given
from hypothesis import strategies as st

from blackbull import Response, JSONResponse, WebSocketResponse
from blackbull.response import cookie_header
from blackbull.app import _wrap_send


# ---------------------------------------------------------------------------
# Response
# ---------------------------------------------------------------------------

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
# _wrap_send
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_wrap_send_unpacks_response():
    """A Response object becomes a standard ASGI start + body event pair."""
    calls = []

    async def raw(event):
        calls.append(event)

    r = Response(b'hi', status=HTTPStatus.CREATED)
    await _wrap_send(raw)(r)
    assert len(calls) == 2
    start, body = calls
    assert start['type'] == 'http.response.start'
    assert start['status'] == int(HTTPStatus.CREATED)
    assert (b'content-type', b'text/html; charset=utf-8') in start['headers']
    assert body == {'type': 'http.response.body', 'body': b'hi', 'more_body': False}


@pytest.mark.asyncio
async def test_wrap_send_unpacks_jsonresponse():
    calls = []

    async def raw(event):
        calls.append(event)

    r = JSONResponse({'ok': True}, status=HTTPStatus.UNAUTHORIZED)
    await _wrap_send(raw)(r)
    assert len(calls) == 2
    start, body = calls
    assert start['status'] == int(HTTPStatus.UNAUTHORIZED)
    assert (b'content-type', b'application/json') in start['headers']
    assert body['body'] == b'{"ok": true}'


@pytest.mark.asyncio
async def test_wrap_send_passes_dict_through():
    """Standard ASGI event dicts forward unchanged to the raw send callable."""
    calls = []

    async def raw(event):
        calls.append(event)

    evt = {'type': 'http.response.start', 'status': 200, 'headers': []}
    await _wrap_send(raw)(evt)
    assert calls[0] is evt


@pytest.mark.asyncio
async def test_wrap_send_passes_bytes_through():
    """Bytes are emitted as a start + body event pair carrying the bytes payload."""
    calls = []

    async def raw(event):
        calls.append(event)

    await _wrap_send(raw)(b'raw bytes')
    assert len(calls) == 2
    assert calls[0]['type'] == 'http.response.start'
    assert calls[1] == {'type': 'http.response.body', 'body': b'raw bytes', 'more_body': False}


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
