"""Request body and cookie helpers.

Provides:

- `read_body`: buffers all ASGI ``http.request`` chunks into a single ``bytes`` object.
- `parse_cookies`: parses the ``Cookie`` request header into a ``dict[str, str]``.
"""


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
