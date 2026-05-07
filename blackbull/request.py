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

    HTTP/2 sends each cookie as a separate header field (RFC 7540 §8.1.2.5);
    this function concatenates all ``cookie`` fields before parsing so that
    HTTP/1.1 (one combined field) and HTTP/2 (multiple fields) are handled
    identically.
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
