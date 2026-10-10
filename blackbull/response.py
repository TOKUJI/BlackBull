"""HTTP and WebSocket response objects. See docs/guide/requests-and-responses.md
for send and streaming contracts.
"""
import json
import logging
from collections.abc import AsyncIterator, Mapping
from http import HTTPStatus

from .native import NativeResponse, _native_from_asgi

logger = logging.getLogger(__name__)


def _normalize_headers(headers) -> list[tuple[bytes, bytes]]:
    """Return a Mapping or pair iterable as ``(bytes, bytes)`` pairs, in order.

    str names/values must encode as ASCII (else UnicodeEncodeError); a bad
    shape raises TypeError.  The field grammar is checked when a
    NativeResponse is built from the pairs, not here.
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


#: ``content-type`` pairs by the ``str`` they came from; bounded,
#: since a content type is almost always a constant of the application.
_CONTENT_TYPE_PAIRS: dict[str, tuple[bytes, bytes]] = {}
_CONTENT_TYPE_PAIRS_MAX = 64


def _content_type_pair(content_type) -> tuple[bytes, bytes]:
    pair = _CONTENT_TYPE_PAIRS.get(content_type) if type(content_type) is str else None
    if pair is None:
        [pair] = _normalize_headers([(b'content-type', content_type)])
        if type(content_type) is str and len(_CONTENT_TYPE_PAIRS) < _CONTENT_TYPE_PAIRS_MAX:
            _CONTENT_TYPE_PAIRS[content_type] = pair
    return pair


async def _emit_response(send, body: bytes, status, headers) -> None:
    """Send one complete NativeResponse with an integer status; *headers* is
    any iterable of pairs, copied and checked as the NativeResponse is built."""
    await send(NativeResponse.complete(int(status), headers, body))


class Response:
    """HTTP body, status and headers; return from a handler or pass to BlackBull send.

    An arbitrary ASGI host's raw send does not accept Response objects.
    """

    def __init__(self, content: str | bytes,
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
        self.headers = [_content_type_pair(content_type)]
        if headers:
            self.headers.extend(_normalize_headers(headers))

    async def __call__(self, conn, receive, send) -> None:
        """Send a complete native response when invoked directly.

        Normalized Response sends use to_native and bypass this method.
        """
        await _emit_response(send, self.body, self.status, self.headers)

    def to_native(self) -> NativeResponse:
        """Convert this response to the unified native message (one send).

        The native-path serialiser: a complete ``Response`` becomes a single
        [`NativeResponse`][blackbull.native.NativeResponse] carrying status, headers,
        and body — one object, one ``send``.  Symmetric with
        [`NativeResponse.to_asgi`][NativeResponse.to_asgi] (the boundary conversion); streaming
        response types drive themselves and are not converted here.
        """
        return NativeResponse.complete(int(self.status), self.headers, self.body)


class JSONResponse(Response):
    """JSON response; return from a handler or pass to BlackBull send.
    """

    def __init__(self, content,
                 status: HTTPStatus = HTTPStatus.OK,
                 headers: Mapping | list | None = None):
        super().__init__(json.dumps(content).encode(), status, 'application/json', headers)


class RedirectResponse(Response):
    """Redirect with an empty body and Location; default status is 302.

    url must be ASCII; percent-encode non-ASCII characters first.
    """

    def __init__(self, url: str,
                 status: HTTPStatus = HTTPStatus.FOUND,
                 headers: Mapping | list | None = None):
        merged = [(b'location', url.encode('ascii')), *_normalize_headers(headers)]
        super().__init__(b'', status=status, headers=merged)


def cookie_header(name: str, value: str, path: str = '/',
                  http_only: bool = True) -> tuple[bytes, bytes]:
    """Build a ``set-cookie`` header tuple for response headers; checked, like
    any field, when the NativeResponse carrying it is built."""
    flags = '; HttpOnly' if http_only else ''
    return (b'set-cookie',
            f'{name}={value}; Path={path}{flags}; SameSite=Lax'.encode())


class StreamingResponse:
    """Stream from an async iterator; finalize the generator on completion or cancellation.

    Return from a handler, pass to BlackBull send, or invoke as (conn, receive, send).
    """

    def __init__(self, content: AsyncIterator,
                 *,
                 status: int = 200,
                 headers: Mapping | list | None = None,
                 media_type: str = 'text/plain'):
        self._content = content
        self._status = status
        self._headers = _normalize_headers(headers)
        self._media_type = _normalize_headers([
            (b'content-type', media_type),
        ])[0][1]

    async def __call__(self, conn, receive, send) -> None:
        h = list(self._headers)
        if not any(k.lower() == b'content-type' for k, _ in h):
            h.insert(0, (b'content-type', self._media_type))
        await send(NativeResponse(status=self._status, header=h))
        async for chunk in self._content:
            if isinstance(chunk, str):
                chunk = chunk.encode()
            if chunk:
                await send(NativeResponse(body=chunk, more_body=True))
        await send(NativeResponse(body=b'', more_body=False))


def _validate_sse_metadata(name: str, value, *, reject_nul: bool = False) -> str:
    """Return one metadata value only when it remains one SSE field."""
    text = str(value)
    if '\r' in text or '\n' in text or (reject_nul and '\x00' in text):
        prohibited = 'CR, LF, or NUL' if reject_nul else 'CR or LF'
        raise ValueError(f'SSE {name} must not contain {prohibited}')
    return text


def _format_sse_event(event) -> bytes:
    """Format one SSE event per the WHATWG HTML Living Standard §9.2.6.

    Accepted shapes:

    * ``str`` / ``bytes`` — a bare message; emitted as ``data: <text>\\n\\n``.
    * ``Mapping`` — fields plucked by key (``data``, ``event``, ``id``,
      ``retry``).  ``data`` may be a string with embedded newlines (each
      line emits its own ``data:`` field per the spec).  Bytes and bytearrays
      use strict UTF-8; other non-string ``data`` is JSON-serialised.  ``id``
      and ``event`` are coerced to string, with CR/LF rejected in both and NUL
      rejected in ``id``; ``retry`` is coerced to int milliseconds.  Unknown
      keys are ignored.

    Returns the encoded UTF-8 bytes; the caller pushes them down a
    [`StreamingResponse`][] (or any ASGI body sink) directly.
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
            value = _validate_sse_metadata('event', ev)
            out += b'event: ' + value.encode('utf-8') + b'\n'
        eid = event.get('id')
        if eid is not None:
            value = _validate_sse_metadata('id', eid, reject_nul=True)
            out += b'id: ' + value.encode('utf-8') + b'\n'
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

    CR, LF, and CRLF split into multiple ``data:`` fields per WHATWG
    §9.2.5, so the client reconstructs logical lines joined by ``\\n``.
    Empty and trailing lines are preserved.
    """
    text = text.replace('\r\n', '\n').replace('\r', '\n')
    return b''.join(b'data: ' + line.encode('utf-8') + b'\n'
                    for line in text.split('\n'))


class EventSourceResponse(StreamingResponse):
    """Format str, UTF-8 bytes or data/event/id/retry mappings as SSE.

    Data line endings are normalized. Reject CR/LF in event/id and NUL in id.
    Defaults to text/event-stream and Cache-Control: no-cache; headers may override.
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


def wrap_native_send(raw_send):
    """Normalize handler and middleware emissions to native HTTP messages.

    Response objects and the ``(bytes, status, headers)`` form become a
    ``NativeResponse``. Streaming responses use the same per-event converter
    as ASGI dictionaries. Unknown events and native messages pass through.
    """
    # Deliberately unannotated: rebuilt per request (see _wrap_send_native in app.py).
    async def _send(event, status=HTTPStatus.OK, headers=()):
        if isinstance(event, StreamingResponse):
            # The nested adapter closes over raw_send, not itself or stream;
            # request adapters must remain reclaimable by refcounting alone.
            await event(None, None, wrap_native_send(raw_send))
        elif isinstance(event, Response):
            await raw_send(event.to_native())
        elif isinstance(event, (bytes, bytearray, memoryview)):
            body = bytes(event) if not isinstance(event, bytes) else event
            await raw_send(NativeResponse(status=int(status),
                                          header=list(headers),
                                          body=body))
        elif isinstance(event, dict):
            await raw_send(_native_from_asgi(event))
        else:
            await raw_send(event)

    return _send
