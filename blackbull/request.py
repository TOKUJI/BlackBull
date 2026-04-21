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
    """Parse the ``Cookie`` request header from an ASGI scope into a dict."""
    raw = scope['headers'].get_value(b'cookie')
    result = {}
    if not raw:
        return result
    for part in raw.split(b';'):
        k, _, v = part.strip().partition(b'=')
        result[k.strip().decode(errors='replace')] = v.strip().decode(errors='replace')
    return result
