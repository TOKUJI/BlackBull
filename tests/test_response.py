import json
from http import HTTPStatus

import pytest

from blackbull import Response, JSONResponse, WebSocketResponse
from blackbull.response import cookie_header
from blackbull.BlackBull import _wrap_send


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


def test_response_raises_for_int():
    with pytest.raises(TypeError):
        Response(12345)


def test_response_raises_for_dict():
    with pytest.raises(TypeError):
        Response({'key': 'value'})


# ---------------------------------------------------------------------------
# JSONResponse
# ---------------------------------------------------------------------------

def test_jsonresponse_body_dict():
    obj = {'x': [0, '']}
    assert JSONResponse(obj).body == json.dumps(obj).encode()


def test_jsonresponse_body_list():
    lst = [1, 'two', 3]
    assert JSONResponse(lst).body == json.dumps(lst).encode()


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
    calls = []

    async def raw(body, status=HTTPStatus.OK, headers=[]):
        calls.append((body, status, headers))

    r = Response(b'hi', status=HTTPStatus.CREATED)
    await _wrap_send(raw)(r)
    assert len(calls) == 1
    body, status, headers = calls[0]
    assert body == b'hi'
    assert status == HTTPStatus.CREATED
    assert (b'content-type', b'text/html; charset=utf-8') in headers


@pytest.mark.asyncio
async def test_wrap_send_unpacks_jsonresponse():
    calls = []

    async def raw(body, status=HTTPStatus.OK, headers=[]):
        calls.append((body, status, headers))

    r = JSONResponse({'ok': True}, status=HTTPStatus.UNAUTHORIZED)
    await _wrap_send(raw)(r)
    body, status, headers = calls[0]
    assert body == b'{"ok": true}'
    assert status == HTTPStatus.UNAUTHORIZED
    assert (b'content-type', b'application/json') in headers


@pytest.mark.asyncio
async def test_wrap_send_passes_dict_through():
    calls = []

    async def raw(event, status=HTTPStatus.OK, headers=[]):
        calls.append(event)

    evt = {'type': 'http.response.start', 'status': 200, 'headers': []}
    await _wrap_send(raw)(evt)
    assert calls[0] is evt


@pytest.mark.asyncio
async def test_wrap_send_passes_bytes_through():
    calls = []

    async def raw(body, status=HTTPStatus.OK, headers=[]):
        calls.append(body)

    await _wrap_send(raw)(b'raw bytes')
    assert calls[0] == b'raw bytes'


# ---------------------------------------------------------------------------
# WebSocketResponse (unchanged)
# ---------------------------------------------------------------------------

def test_WebSocketResponse_str():
    result = WebSocketResponse('hello')
    assert result == {'type': 'websocket.send', 'text': 'hello'}


def test_WebSocketResponse_bytes():
    result = WebSocketResponse(b'\xde\xad\xbe\xef')
    assert result == {'type': 'websocket.send', 'bytes': b'\xde\xad\xbe\xef'}


def test_WebSocketResponse_dict_is_json_encoded():
    obj = {'key': 'value', 'num': 42}
    result = WebSocketResponse(obj)
    assert result == {'type': 'websocket.send', 'text': json.dumps(obj)}


def test_WebSocketResponse_list_is_json_encoded():
    lst = [1, 'two', 3]
    result = WebSocketResponse(lst)
    assert result == {'type': 'websocket.send', 'text': json.dumps(lst)}


def test_WebSocketResponse_empty_string():
    assert WebSocketResponse('') == {'type': 'websocket.send', 'text': ''}


def test_WebSocketResponse_plain_string_is_not_json_encoded():
    name = 'Toshio'
    result = WebSocketResponse(name)
    assert result['text'] == name


def test_WebSocketResponse_plain_string_has_no_json_quotes():
    result = WebSocketResponse('hello')
    assert result['text'] == 'hello'
    assert result['text'] != '"hello"'


def test_WebSocketResponse_bytes_is_not_json_encoded():
    payload = b'\xde\xad\xbe\xef'
    result = WebSocketResponse(payload)
    assert result.get('bytes') == payload


def test_WebSocketResponse_uses_text_field_for_str():
    result = WebSocketResponse('world')
    assert 'text' in result
    assert result.get('type') == 'websocket.send'


def test_WebSocketResponse_uses_bytes_field_for_bytes():
    result = WebSocketResponse(b'raw')
    assert 'bytes' in result
    assert result.get('type') == 'websocket.send'
