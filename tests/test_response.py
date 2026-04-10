import json

import pytest

from blackbull import Response, JSONResponse, WebSocketResponse


# ---------------------------------------------------------------------------
# Response
# ---------------------------------------------------------------------------

def test_Response_bytes():
    assert Response(b'text') == b'text'


def test_Response_str():
    assert Response('text') == b'text'


def test_Response_raises_for_non_str_bytes():
    with pytest.raises(TypeError):
        Response(12345)


def test_Response_raises_for_dict():
    with pytest.raises(TypeError):
        Response({'key': 'value'})


# ---------------------------------------------------------------------------
# JSONResponse
# ---------------------------------------------------------------------------

def test_JSONResponse():
    obj = {'x': [0, '']}
    assert JSONResponse(obj) == json.dumps(obj).encode()


def test_JSONResponse_list():
    lst = [1, 'two', 3]
    assert JSONResponse(lst) == json.dumps(lst).encode()


# ---------------------------------------------------------------------------
# WebSocketResponse
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


# ---------------------------------------------------------------------------
# Bug 9 – WebSocketResponse must NOT double-encode plain strings
# ---------------------------------------------------------------------------
#
# Bug description: the old implementation called ``json.dumps(content)`` even
# when ``content`` was already a plain ``str``.  For a string like ``'Toshio'``
# that produced ``'"Toshio"'`` (with surrounding double-quotes), so the client
# received ``"Toshio"`` instead of ``Toshio``.

def test_WebSocketResponse_plain_string_is_not_json_encoded():
    """Bug 9 regression: a plain str must arrive as-is, without extra quotes."""
    name = 'Toshio'
    result = WebSocketResponse(name)
    assert result['text'] == name, (
        f"Expected text={name!r}, got text={result['text']!r}. "
        "WebSocketResponse must NOT apply json.dumps() to plain strings."
    )


def test_WebSocketResponse_plain_string_has_no_json_quotes():
    """Bug 9: the client-visible string must not be wrapped in extra quotes."""
    result = WebSocketResponse('hello')
    assert result['text'] == 'hello'
    assert result['text'] != '"hello"'


def test_WebSocketResponse_bytes_is_not_json_encoded():
    """Bug 9 (bytes variant): raw bytes must not be JSON-serialised."""
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
