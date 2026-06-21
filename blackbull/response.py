"""HTTP and WebSocket response objects.

Provides:

- `Response`: plain HTTP response (HTML / plain text / binary).
- `JSONResponse`: convenience subclass that serialises a Python object to JSON.
- `StreamingResponse`: pushes an async iterator to the client without buffering.
- `EventSourceResponse`: WHATWG Server-Sent Events on top of StreamingResponse.
- `WebSocketResponse`: wraps text, bytes, or dict data as a WebSocket send event.
- `cookie_header`: builds a ``(b'set-cookie', ...)`` header tuple with secure defaults.
"""
import json
from collections.abc import AsyncIterator, Mapping
from http import HTTPStatus
from typing import Union

import logging
logger = logging.getLogger(__name__)


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
            # ASGI requires headers as bytes/bytes tuples; coerce str on the
            # way in so callers may pass either shape without crashing the
            # sender's b''.join later.  ASCII per RFC 9110 §5.5 — non-ASCII
            # input raises UnicodeEncodeError at construction rather than
            # letting obs-text bytes onto the wire.
            for k, v in headers:
                if isinstance(k, str):
                    k = k.encode('ascii')
                if isinstance(v, str):
                    v = v.encode('ascii')
                self.headers.append((k, v))

    async def __call__(self, scope, receive, send) -> None:
        """Drive this response as an ASGI app: emit ``start`` then ``body``.

        Mirrors :class:`StreamingResponse` so every BlackBull response type
        shares one protocol — ``await response(scope, receive, send)`` —
        whether returned from a simplified handler, invoked explicitly by a
        full-form handler, or normalised by ``app._wrap_send``.  Keeping the
        start/body serialisation here means there is a single source of truth
        for turning a Response into ASGI events.
        """
        await send({'type': 'http.response.start',
                    'status': int(self.status),
                    'headers': list(self.headers)})
        await send({'type': 'http.response.body',
                    'body': self.body, 'more_body': False})


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


class StreamingResponse:
    """Stream a response body from an async generator.

    Usage::

        async def lines():
            for i in range(10):
                yield f'line {i}\\n'.encode()
                await asyncio.sleep(0.1)

        @app.route(path='/stream')
        async def handler(scope, receive, send):
            await StreamingResponse(lines())(scope, receive, send)
    """

    def __init__(self, content: AsyncIterator,
                 *,
                 status: int = 200,
                 headers: list | None = None,
                 media_type: str = 'text/plain'):
        self._content = content
        self._status = status
        self._headers = list(headers or [])
        self._media_type = media_type

    async def __call__(self, scope, receive, send) -> None:
        h = list(self._headers)
        if not any(k.lower() == b'content-type' for k, _ in h):
            h.insert(0, (b'content-type', self._media_type.encode()))
        await send({'type': 'http.response.start', 'status': self._status, 'headers': h})
        async for chunk in self._content:
            if isinstance(chunk, str):
                chunk = chunk.encode()
            if chunk:
                await send({'type': 'http.response.body', 'body': chunk, 'more_body': True})
        await send({'type': 'http.response.body', 'body': b'', 'more_body': False})


def _format_sse_event(event) -> bytes:
    """Format one SSE event per the WHATWG HTML Living Standard §9.2.6.

    Accepted shapes:

    * ``str`` / ``bytes`` — a bare message; emitted as ``data: <text>\\n\\n``.
    * ``Mapping`` — fields plucked by key (``data``, ``event``, ``id``,
      ``retry``).  ``data`` may be a string with embedded newlines (each
      line emits its own ``data:`` field per the spec); a non-string
      ``data`` is JSON-serialised.  ``id`` and ``event`` are coerced to
      string; ``retry`` to int milliseconds.  Unknown keys are ignored.

    Returns the encoded UTF-8 bytes; the caller pushes them down a
    :class:`StreamingResponse` (or any ASGI body sink) directly.
    """
    if isinstance(event, bytes):
        text = event.decode('utf-8')
        return _sse_data_lines(text) + b'\n'
    if isinstance(event, str):
        return _sse_data_lines(event) + b'\n'
    if isinstance(event, Mapping):
        out = bytearray()
        ev = event.get('event')
        if ev is not None:
            out += b'event: ' + str(ev).encode('utf-8') + b'\n'
        eid = event.get('id')
        if eid is not None:
            out += b'id: ' + str(eid).encode('utf-8') + b'\n'
        retry = event.get('retry')
        if retry is not None:
            out += b'retry: ' + str(int(retry)).encode('ascii') + b'\n'
        data = event.get('data')
        if data is not None:
            if isinstance(data, (bytes, bytearray)):
                payload = bytes(data).decode('utf-8')
            elif isinstance(data, str):
                payload = data
            else:
                payload = json.dumps(data)
            out += _sse_data_lines(payload)
        out += b'\n'
        return bytes(out)
    raise TypeError(
        f"SSE event must be str, bytes, or Mapping; got {type(event).__name__}")


def _sse_data_lines(text: str) -> bytes:
    """Encode *text* as one or more ``data: ...\\n`` lines.

    Embedded ``\\n`` characters split into multiple ``data:`` fields per
    WHATWG §9.2.6 so the client reconstructs the original payload by
    joining the lines with ``\\n``.  Trailing newline is preserved by the
    splitlines semantics.
    """
    return b''.join(b'data: ' + line.encode('utf-8') + b'\n'
                    for line in text.split('\n'))


class EventSourceResponse(StreamingResponse):
    """Stream a Server-Sent Events response from an async iterator.

    Yields are formatted per WHATWG §9.2.6 (the EventSource spec).  Each
    item produced by *content* may be a ``str`` (bare data), ``bytes``
    (bare data, UTF-8), or a ``Mapping`` with optional ``data`` /
    ``event`` / ``id`` / ``retry`` keys.

    The content-type is forced to ``text/event-stream`` and
    ``Cache-Control: no-cache`` is auto-emitted; both are overridable
    via the *headers* argument if a deployment knows what it's doing.

    Usage::

        async def tokens():
            yield {'event': 'token', 'data': 'hello'}
            yield {'event': 'token', 'data': 'world'}
            yield {'event': 'done',  'data': ''}

        @app.route(path='/sse')
        async def stream():
            return EventSourceResponse(tokens())
    """

    def __init__(self, content: AsyncIterator,
                 *,
                 status: int = 200,
                 headers: list | None = None):
        # Caller-provided Cache-Control / Content-Type wins (case-insensitive).
        h = list(headers or [])
        if not any(k.lower() == b'cache-control' for k, _ in h):
            h.append((b'cache-control', b'no-cache'))
        super().__init__(
            self._encode(content),
            status=status, headers=h,
            media_type='text/event-stream',
        )

    @staticmethod
    async def _encode(events):
        """Translate a stream of events into pre-formatted SSE byte chunks."""
        async for event in events:
            yield _format_sse_event(event)


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
