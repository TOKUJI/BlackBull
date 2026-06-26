"""Request body and cookie helpers.

Provides:

- `read_body`: buffers all ASGI ``http.request`` chunks into a single ``bytes`` object.
- `read_json`: buffers the body and parses it as JSON (``None`` on empty/invalid).
- `read_text`: buffers the body and decodes it as text.
- `parse_cookies`: parses the ``Cookie`` request header into a ``dict[str, str]``.
"""
import json
from typing import Any


async def read_body(receive) -> bytes:
    """Read the complete request body from the ASGI receive channel.

    Collects chunks in a list and joins once, rather than the O(n²) ``+=``
    growth.  A single-chunk body (the common case) is returned directly with
    no intermediate copy at all (copy-reduction-http1 P1).
    """
    chunks: list[bytes] = []
    while True:
        event = await receive()
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
    """
    body = await read_body(receive)
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
    """
    body = await read_body(receive)
    return body.decode(encoding, errors='replace')


def parse_cookies(scope) -> dict[str, str]:
    """Parse the ``Cookie`` request header from an ASGI scope into a dict.

    Works identically for HTTP/1.1, HTTP/2, and WebSocket scopes.
    HTTP/1.1 sends a single combined ``Cookie`` header; HTTP/2 may split it
    into multiple fields (RFC 7540 §8.1.2.5).  All ``cookie`` fields are
    collected and joined before parsing, so both wire formats produce the
    same result.

    Accepts either of the two header shapes that may appear on
    ``scope['headers']``:

    - A plain list/iterable of ``(name, value)`` bytes tuples — the
      standard ASGI 3.0 form, used by external servers (uvicorn,
      hypercorn, ``httpx.ASGITransport``).
    - A :class:`blackbull.headers.Headers` instance — what BlackBull's
      own server attaches as a handler ergonomics enhancement.
    """
    headers = scope.get('headers', ())
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
