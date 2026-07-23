"""Request body and cookie helpers.

Provides:

- `read_body`: buffers all ASGI ``http.request`` chunks into a single ``bytes`` object.
- `read_json`: buffers the body and parses it as JSON (``None`` on empty/invalid).
- `read_text`: buffers the body and decodes it as text.
- `cookies_from_headers`: parses the ``Cookie`` header(s) into a ``dict[str, str]``
  straight from a headers object — the native core (what ``Connection.cookies`` uses).
- `parse_cookies`: the ASGI-scope-shaped wrapper of the above, for external
  callers that hold a scope dict.

The opt-in HTTP context object formerly named ``Request`` moved to
:class:`blackbull.connection.Connection` (Sprint 79 Phase 5); ``Request`` is now
a deprecated alias of ``Connection`` (see ``blackbull.__getattr__``). This
module holds only the transport-agnostic free functions, which
:class:`Connection` builds on.
"""
import json
from typing import Any

from .asgi import ASGIEvent


class ClientDisconnected(Exception):
    """Raised when the client disconnects before the request body is complete.

    ASGI signals a mid-body disconnect with an ``http.disconnect`` event that
    carries no ``body``/``more_body`` keys.  Treating it as end-of-message
    would return a *truncated* upload as if it were whole, so
    :func:`read_body` raises this instead — the handler must not process a
    partial body as complete.  The ``partial`` attribute holds whatever body
    bytes had arrived before the disconnect.
    """

    def __init__(self, partial: bytes = b''):
        super().__init__('client disconnected before the request body completed')
        self.partial = partial


async def read_body(receive) -> bytes:
    """Read the complete request body from the ASGI receive channel.

    Collects chunks in a list and joins once, rather than the O(n²) ``+=``
    growth.  A single-chunk body (the common case) is returned directly with
    no intermediate copy at all (copy-reduction-http1 P1).

    Raises :class:`ClientDisconnected` if an ``http.disconnect`` arrives
    before the body is complete, so a truncated upload is never silently
    returned as if whole.
    """
    chunks: list[bytes] = []
    while True:
        event = await receive()
        if event.get('type') == ASGIEvent.HTTP_DISCONNECT:
            # Peer went away mid-body — the accumulated bytes are a partial
            # upload, not a complete one.  Surface it rather than returning
            # the truncated body as if it were the whole message.
            raise ClientDisconnected(b''.join(chunks))
        chunk = event.get('body', b'')
        if chunk:
            chunks.append(chunk)
        if not event.get('more_body', False):
            break
    if not chunks:
        return b''
    if len(chunks) == 1:
        return chunks[0]
    return b''.join(chunks)


async def read_json(receive) -> Any:
    """Read the request body and parse it as JSON.

    Returns the parsed JSON value (``dict``, ``list``, ``str``, ``int``,
    ``float``, ``bool``), or ``None`` when the body is empty, not valid JSON,
    or not decodable.  Callers should treat ``None`` as a client error and
    respond ``400``::

        data = await read_json(receive)
        if data is None:
            await send(JSONResponse({'error': 'invalid JSON'},
                                    status=HTTPStatus.BAD_REQUEST))
            return

    Note that a literal JSON ``null`` body also parses to ``None``; if that
    distinction matters, read the body yourself with :func:`read_body`.

    A mid-body client disconnect propagates as :class:`ClientDisconnected`
    rather than being reported as invalid JSON — a truncated body is a
    transport failure, not a parse error.
    """
    body = await read_body(receive)
    return _json_or_none(body)


def _json_or_none(body: bytes) -> Any:
    """Parse *body* as JSON, returning ``None`` on empty/invalid input."""
    if not body:
        return None
    try:
        return json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None


async def read_text(receive, encoding: str = 'utf-8') -> str:
    """Read the request body and decode it as text.

    Uses ``errors='replace'`` so undecodable bytes become U+FFFD rather than
    raising — a malformed body never crashes the handler.  Override
    *encoding* for non-UTF-8 payloads.

    A mid-body client disconnect still propagates as
    :class:`ClientDisconnected`: a truncated upload must not be decoded and
    returned as if it were the complete text.
    """
    body = await read_body(receive)
    return body.decode(encoding, errors='replace')


def parse_cookies(source) -> dict[str, str]:
    """Parse the ``Cookie`` request header into a dict.

    *source* is a mapping carrying a ``'headers'`` key — an ASGI scope dict, or
    the ``{'headers': conn.headers}`` wrapper ``Connection.cookies`` passes.
    Works identically for HTTP/1.1, HTTP/2, and WebSocket. HTTP/1.1 sends a
    single combined ``Cookie`` header; HTTP/2 may split it into multiple fields
    (RFC 7540 §8.1.2.5).  All ``cookie`` fields are collected and joined before
    parsing, so both wire formats produce the same result.

    Accepts either of the two header shapes that may appear on
    ``source['headers']``:

    - A plain list/iterable of ``(name, value)`` bytes tuples — the
      standard ASGI 3.0 form, used by external servers (uvicorn,
      hypercorn, ``httpx.ASGITransport``).
    - A :class:`blackbull.headers.Headers` instance — what BlackBull's
      own server attaches as a handler ergonomics enhancement.
    """
    return cookies_from_headers(source.get('headers', ()))


def cookies_from_headers(headers) -> dict[str, str]:
    """Parse the ``Cookie`` header(s) into a dict, straight from a headers
    object/iterable — no ASGI scope dict involved.

    This is the native core: :meth:`Connection.cookies` calls it directly on
    ``conn.headers``; :func:`parse_cookies` is the ASGI-scope-shaped wrapper kept
    for external callers that hold a scope dict. Accepts a
    :class:`blackbull.headers.Headers` instance (uses ``getlist``) or a plain
    iterable of ``(name, value)`` bytes tuples (the ASGI 3.0 form)."""
    getlist = getattr(headers, 'getlist', None)
    if getlist is not None:
        cookie_values = [v for _, v in getlist(b'cookie')]
    else:
        cookie_values = [v for (k, v) in headers if k.lower() == b'cookie']
    if not cookie_values:
        return {}
    raw = b'; '.join(cookie_values)
    result = {}
    for part in raw.split(b';'):
        k, _, v = part.strip().partition(b'=')
        result[k.strip().decode(errors='replace')] = v.strip().decode(errors='replace')
    return result
