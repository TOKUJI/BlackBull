import json
from http import HTTPStatus
from typing import Union

from .logger import get_logger_set
logger, log = get_logger_set('response')


class Response:
    """HTTP response object carrying body, status, and headers.

    Pass directly to the ASGI ``send`` callable when using BlackBull::

        await send(Response('<h1>Hello</h1>'))
        await send(Response(b'data', status=HTTPStatus.NOT_FOUND))
    """

    def __init__(self, content: Union[str, bytes],
                 status: HTTPStatus = HTTPStatus.OK,
                 content_type: str = 'text/html; charset=utf-8',
                 headers: list | None = None):
        if isinstance(content, str):
            self.body = content.encode()
        elif isinstance(content, bytes):
            self.body = content
        else:
            raise TypeError(f'Response expects str or bytes, got {type(content)}')
        self.status = status
        self.headers = [(b'content-type', content_type.encode())]
        if headers:
            self.headers.extend(headers)


class JSONResponse(Response):
    """HTTP response with JSON-serialised body and ``application/json`` content-type.

    Pass directly to the ASGI ``send`` callable when using BlackBull::

        await send(JSONResponse({'ok': True}))
        await send(JSONResponse({'error': 'Not found'}, status=HTTPStatus.NOT_FOUND))
    """

    def __init__(self, content,
                 status: HTTPStatus = HTTPStatus.OK,
                 headers: list | None = None):
        super().__init__(json.dumps(content).encode(), status, 'application/json', headers)


def cookie_header(name: str, value: str, path: str = '/',
                  http_only: bool = True) -> tuple[bytes, bytes]:
    """Build a ``set-cookie`` header tuple suitable for inclusion in response headers."""
    flags = '; HttpOnly' if http_only else ''
    return (b'set-cookie', f'{name}={value}; Path={path}{flags}; SameSite=Lax'.encode())


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
