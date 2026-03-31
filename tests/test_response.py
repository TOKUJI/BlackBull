import json
from http import HTTPStatus

from blackbull.logger import get_logger_set

# Library for test-fixture
# from multiprocessing import Process
import asyncio
import pytest
import pytest_asyncio

# Test targets
from blackbull import Response, JSONResponse, WebSocketResponse
from blackbull.response import make_body, make_websocket_body

# Library for tests
# import httpx

logger, log = get_logger_set()


class Dummy:
    """
    Mock of send object. It holds data in its instance.
    """
    def __init__(self, *args, **kwargs):
        self.data = None

    async def __call__(self, data):
        self.data = data


@pytest_asyncio.fixture
async def send():
    logger.info('At teardown.')

    send = Dummy()
    yield send

    logger.info('At teardown.')


@pytest.mark.asyncio
async def test_Response(send):
    assert send.data != b'text'

    await Response(send, b'text')

    assert send.data == make_body(b'text')


@pytest.mark.asyncio
async def test_Response_str(send):
    assert send.data != b'text'

    await Response(send, 'text')

    assert send.data == make_body(b'text')


@pytest.mark.asyncio
async def test_JSONResponse(send):
    assert send.data != b'text'

    obj = {'x': list(set([str(), int()]))}   # Example of complicated object.

    await JSONResponse(send, obj)

    assert send.data == make_body(json.dumps(obj).encode())


@pytest.mark.asyncio
async def test_WebSocketResponse(send):
    assert send.data != b'text'

    obj = {'x': list(set([str(), int()]))}   # Example of complicated object.

    await WebSocketResponse(send, obj)

    assert send.data == make_websocket_body(json.dumps(obj))


# ---------------------------------------------------------------------------
# Bug 9 – WebSocketResponse must NOT double-encode plain strings
# ---------------------------------------------------------------------------
#
# Bug description: the old implementation called ``json.dumps(content)`` even
# when ``content`` was already a plain ``str``.  For a string like ``'Toshio'``
# that produced ``'"Toshio"'`` (with surrounding double-quotes), so the client
# received ``"Toshio"`` instead of ``Toshio``.  The test
# ``test_websocket_response`` in test_BlackBull.py would have been the first
# integration test to catch this, but unit tests here exercise the function in
# isolation and are cheaper to run.

@pytest.mark.asyncio
async def test_WebSocketResponse_plain_string_is_not_json_encoded(send):
    """Bug 9 regression: a plain str must arrive as-is, without extra quotes.

    ``json.dumps('Toshio')`` returns ``'"Toshio"'`` (a 8-char string with
    surrounding double-quotes).  The client would receive ``"Toshio"`` instead
    of ``Toshio``.  WebSocketResponse must detect ``str`` and pass it through
    directly.
    """
    name = 'Toshio'
    await WebSocketResponse(send, name)

    # The text field in the body must be the *raw* string, not its JSON repr.
    assert send.data['text'] == name, (
        f"Expected text={name!r}, got text={send.data['text']!r}. "
        "WebSocketResponse must NOT apply json.dumps() to plain strings."
    )


@pytest.mark.asyncio
async def test_WebSocketResponse_plain_string_has_no_json_quotes(send):
    """Bug 9: the client-visible string must not be wrapped in extra quotes."""
    await WebSocketResponse(send, 'hello')
    # json.dumps('hello') == '"hello"' (9 chars with surrounding double-quotes).
    # The plain text should be 5 chars, not 7.
    assert send.data['text'] == 'hello'
    assert send.data['text'] != '"hello"'


@pytest.mark.asyncio
async def test_WebSocketResponse_bytes_is_not_json_encoded(send):
    """Bug 9 (bytes variant): raw bytes must not be JSON-serialised."""
    payload = b'\xde\xad\xbe\xef'
    await WebSocketResponse(send, payload)
    assert send.data.get('bytes') == payload, (
        "WebSocketResponse must pass bytes through unmodified."
    )


@pytest.mark.asyncio
async def test_WebSocketResponse_dict_is_json_encoded(send):
    """Dicts and other non-str/bytes objects must still be JSON-serialised."""
    obj = {'key': 'value', 'num': 42}
    await WebSocketResponse(send, obj)
    assert send.data['text'] == json.dumps(obj)


@pytest.mark.asyncio
async def test_WebSocketResponse_list_is_json_encoded(send):
    """Lists must be JSON-serialised."""
    lst = [1, 'two', 3]
    await WebSocketResponse(send, lst)
    assert send.data['text'] == json.dumps(lst)


@pytest.mark.asyncio
async def test_WebSocketResponse_empty_string(send):
    """An empty string must pass through as an empty string."""
    await WebSocketResponse(send, '')
    assert send.data['text'] == ''


@pytest.mark.asyncio
async def test_WebSocketResponse_uses_text_field_for_str(send):
    """String content must be put in the 'text' field of the ASGI event dict."""
    await WebSocketResponse(send, 'world')
    assert 'text' in send.data
    assert send.data.get('type') == 'websocket.send'


@pytest.mark.asyncio
async def test_WebSocketResponse_uses_bytes_field_for_bytes(send):
    """Bytes content must be put in the 'bytes' field of the ASGI event dict."""
    await WebSocketResponse(send, b'raw')
    assert 'bytes' in send.data
    assert send.data.get('type') == 'websocket.send'


# ---------------------------------------------------------------------------
# Response – TypeError for non-str/bytes content
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_Response_raises_for_non_str_bytes(send):
    """Response() must raise TypeError when content is neither str nor bytes."""
    with pytest.raises(TypeError):
        await Response(send, 12345)


@pytest.mark.asyncio
async def test_Response_raises_for_dict(send):
    """Response() must raise TypeError for dicts (use JSONResponse instead)."""
    with pytest.raises(TypeError):
        await Response(send, {'key': 'value'})
