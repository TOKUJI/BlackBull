"""Request body and cookie helpers.

Provides:

- `read_body`: buffers all ASGI ``http.request`` chunks into a single ``bytes`` object.
- `parse_cookies`: parses the ``Cookie`` request header into a ``dict[str, str]``.
"""


async def read_body(receive) -> bytes:
    """Read the complete request body from the ASGI receive channel."""
    body = b''
    while True:
        event = await receive()
        body += event.get('body', b'')
        if not event.get('more_body', False):
            break
    return body


def parse_cookies(scope) -> dict[str, str]:
    """Parse the ``Cookie`` request header from an ASGI scope into a dict.

    Works identically for HTTP/1.1, HTTP/2, and WebSocket scopes.
    HTTP/1.1 sends a single combined ``Cookie`` header; HTTP/2 may split it
    into multiple fields (RFC 7540 §8.1.2.5).  This function uses
    ``Headers.getlist`` to collect all ``cookie`` fields before joining and
    parsing, so both wire formats produce the same result.
    """
    # getlist returns [(name, value), ...]; join all values with "; "
    cookie_pairs = scope['headers'].getlist(b'cookie')
    if not cookie_pairs:
        return {}
    raw = b'; '.join(v for _, v in cookie_pairs)
    result = {}
    for part in raw.split(b';'):
        k, _, v = part.strip().partition(b'=')
        result[k.strip().decode(errors='replace')] = v.strip().decode(errors='replace')
    return result
