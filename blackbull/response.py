import json
from typing import Union
from http import HTTPStatus

from .logger import get_logger_set
logger, log = get_logger_set('response')


def Response(content: Union[str, bytes]) -> bytes:
    """Encode *content* to bytes for use as an HTTP response body.

    Pass the result directly to the ASGI ``send`` callable::

        await send(Response('Hello'), HTTPStatus.OK)
    """
    if isinstance(content, str):
        return content.encode()
    if isinstance(content, bytes):
        return content
    raise TypeError(f'Response expects str or bytes, got {type(content)}')


def JSONResponse(content) -> bytes:
    """Serialize *content* to JSON bytes for use as an HTTP response body.

    Pass the result directly to the ASGI ``send`` callable::

        await send(JSONResponse({'key': 'value'}), HTTPStatus.OK)
    """
    return json.dumps(content).encode()


def WebSocketResponse(content) -> dict:
    """Build an ASGI ``websocket.send`` event dict from *content*.

    - ``str``  → ``{'type': 'websocket.send', 'text': content}``
    - ``bytes`` → ``{'type': 'websocket.send', 'bytes': content}``
    - anything else → JSON-serialised into the ``text`` field

    Pass the result directly to the ASGI ``send`` callable::

        await send(WebSocketResponse('hello'))
    """
    if isinstance(content, str):
        return {'type': 'websocket.send', 'text': content}
    if isinstance(content, bytes):
        return {'type': 'websocket.send', 'bytes': content}
    return {'type': 'websocket.send', 'text': json.dumps(content)}
