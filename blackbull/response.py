"""HTTP and WebSocket response objects.

Provides:

- `Response`: plain HTTP response (HTML / plain text / binary).
- `JSONResponse`: convenience subclass that serialises a Python object to JSON.
- `RedirectResponse`: convenience subclass that sets a ``Location`` header + 3xx status.
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


def _normalize_headers(headers) -> list[tuple[bytes, bytes]]:
    """Coerce a user-supplied ``headers=`` argument to ASGI's wire shape.

    Accepts either

    * a :class:`~collections.abc.Mapping` (``dict``-like) — matching the
      FastAPI / Starlette / httpx convention, iterated by ``.items()``; or
    * an iterable of ``(name, value)`` pairs (BlackBull's original shape).

    Names and values may be ``str`` (encoded ASCII per RFC 9110 §5.5, so
    non-ASCII raises ``UnicodeEncodeError`` at construction rather than
    letting obs-text bytes onto the wire) or ``bytes``.  Any other shape —
    a bare string, an iterable of non-pairs, a non-bytes/str value — raises
    ``TypeError`` here, at construction, instead of silently corrupting the
    response (the old ``for k, v in headers`` loop iterated a ``dict``'s
    *keys* and unpacked each key string into ``(k, v)``) or blowing up later
    in the sender's ``b''.join``.
    """
    if not headers:
        return []
    items = headers.items() if isinstance(headers, Mapping) else headers
    out: list[tuple[bytes, bytes]] = []
    for pair in items:
        if isinstance(pair, (str, bytes, bytearray)):
            raise TypeError(
                'Response headers must be a mapping or an iterable of '
                f'(name, value) pairs; got a bare {type(pair).__name__} '
                f'element {pair!r}')
        try:
            k, v = pair
        except (TypeError, ValueError):
            raise TypeError(
                'each header must be a (name, value) pair; '
                f'got {pair!r}') from None
        if isinstance(k, str):
            k = k.encode('ascii')
        if isinstance(v, str):
            v = v.encode('ascii')
        if not isinstance(k, (bytes, bytearray)) or not isinstance(v, (bytes, bytearray)):
            raise TypeError(
                'header name and value must be str or bytes; got '
                f'({type(k).__name__}, {type(v).__name__})')
        out.append((bytes(k), bytes(v)))
    return out


async def _emit_response(send, body: bytes, status, headers) -> None:
    """Send a complete non-streamed HTTP response as ASGI ``start`` + ``body``.

    The single source of truth for the ``http.response.start`` /
    ``http.response.body`` event pair used by every non-streaming response
    path — :meth:`Response.__call__`, the app's ``send(body, status, headers)``
    convenience form (``_wrap_send``), and the default error handler.  *status*
    may be an ``int`` or an ``HTTPStatus`` (coerced to ``int`` for the wire);
    *headers* is any iterable of ``(bytes, bytes)`` pairs (copied defensively).
    """
    await send({'type': 'http.response.start',
                'status': int(status),
                'headers': list(headers)})
    await send({'type': 'http.response.body',
                'body': body, 'more_body': False})


class Response:
    """HTTP response object carrying body, status, and headers.

    Pass directly to the ASGI ``send`` callable when using BlackBull::

        await send(Response('<h1>Hello</h1>'))
        await send(Response(b'data', status=HTTPStatus.NOT_FOUND))
    """

    def __init__(self, content: Union[str, bytes],
                 status: HTTPStatus = HTTPStatus.OK,
                 content_type: str = 'text/html; charset=utf-8',
                 headers: Mapping | list | None = None):
        if isinstance(content, str):
            self.body = content.encode()
        elif isinstance(content, bytes):
            self.body = content
        else:
            raise TypeError(f'Response expects str or bytes, got {type(content)}')
        self.status = status
        # A dict or a list of (name, value) pairs; str or bytes names/values.
        # See _normalize_headers for the accepted shapes and the ASCII /
        # RFC 9110 §5.5 coercion rules.
        self.headers = [(b'content-type', content_type.encode())]
        self.headers.extend(_normalize_headers(headers))

    async def __call__(self, conn, receive, send) -> None:
        """Drive this response as an ASGI app: emit ``start`` then ``body``.

        Mirrors :class:`StreamingResponse` so every BlackBull response type
        shares one protocol — ``await response(conn, receive, send)`` —
        whether returned from a simplified handler, invoked explicitly by a
        full-form handler, or normalised by ``app._wrap_send``.  Keeping the
        start/body serialisation here means there is a single source of truth
        for turning a Response into ASGI events.
        """
        await _emit_response(send, self.body, self.status, self.headers)


class JSONResponse(Response):
    """HTTP response with JSON-serialised body and ``application/json`` content-type.

    Pass directly to the ASGI ``send`` callable when using BlackBull::

        await send(JSONResponse({'ok': True}))
        await send(JSONResponse({'error': 'Not found'}, status=HTTPStatus.NOT_FOUND))
    """

    def __init__(self, content,
                 status: HTTPStatus = HTTPStatus.OK,
                 headers: Mapping | list | None = None):
        super().__init__(json.dumps(content).encode(), status, 'application/json', headers)


class RedirectResponse(Response):
    """HTTP redirect response carrying a ``Location`` header.

    Completes the ``Response`` convenience family alongside ``JSONResponse``.
    The body is empty; *url* becomes the ``Location`` header value and *status*
    a 3xx redirect code (default ``302 Found`` — the safer general-purpose
    default, since it does not force the client to preserve the request method).

    Pass directly to the ASGI ``send`` callable, or return it from a handler::

        await send(RedirectResponse('/new-url'))
        return RedirectResponse('/permanent', status=HTTPStatus.MOVED_PERMANENTLY)

    *url* must be ASCII (RFC 9110 §10.2.2 — the Location field value is a
    URI-reference); percent-encode non-ASCII URLs before passing them in.
    """

    def __init__(self, url: str,
                 status: HTTPStatus = HTTPStatus.FOUND,
                 headers: Mapping | list | None = None):
        merged = [(b'location', url.encode('ascii')), *_normalize_headers(headers)]
        super().__init__(b'', status=status, headers=merged)


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
        async def handler(conn, receive, send):
            await StreamingResponse(lines())(conn, receive, send)
    """

    def __init__(self, content: AsyncIterator,
                 *,
                 status: int = 200,
                 headers: Mapping | list | None = None,
                 media_type: str = 'text/plain'):
        self._content = content
        self._status = status
        self._headers = _normalize_headers(headers)
        self._media_type = media_type

    async def __call__(self, conn, receive, send) -> None:
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
                 headers: Mapping | list | None = None):
        # Caller-provided Cache-Control / Content-Type wins (case-insensitive).
        h = _normalize_headers(headers)
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
