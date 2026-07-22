import ipaddress

from ..connection import CONNECTION_STASH_KEY, Connection
from ..headers import Headers

_MISS = object()


def _parse_forwarded(value: str) -> dict[str, str]:
    """Parse the leftmost element of an RFC 7239 Forwarded header value.

    RFC 7239 §4 separates forwarded *elements* with ``,`` and the
    parameters within one element with ``;``.  A chained-proxy header
    such as ``for=203.0.113.1;proto=https, for=198.51.100.17`` therefore
    carries two elements; we honour the leftmost (the hop closest to the
    client) and return its parameters, e.g.
    ``{'for': '203.0.113.1', 'proto': 'https'}``.

    Splitting on ``;`` alone (the pre-Sprint-69 behaviour) folded the
    second element's ``for=`` into the first value, poisoning
    ``scope['client']``.
    """
    first_element = value.split(',', 1)[0]
    result = {}
    for part in first_element.split(';'):
        part = part.strip()
        if '=' in part:
            k, v = part.split('=', 1)
            result[k.strip().lower()] = v.strip().strip('"')
    return result


class TrustedProxy:
    """Rewrite ``scope['client']`` and ``scope['scheme']`` from proxy headers.

    Applied only when the direct TCP peer matches the configured trusted set,
    preventing malicious clients from spoofing ``X-Forwarded-For``.

    Supported headers (in precedence order):

    1. RFC 7239 ``Forwarded`` — ``for=<ip>; proto=<scheme>``
    2. ``X-Forwarded-For`` — comma-separated IP chain; leftmost non-trusted IP wins
    3. ``X-Forwarded-Proto`` — rewrite ``scope['scheme']``

    Args:
        trusted_proxies: IP addresses or CIDR strings (IPv4 or IPv6).  Accepts a
            single string or a list.  Defaults to loopback (``'127.0.0.1'``, ``'::1'``).

    Usage::

        app = BlackBull(trusted_proxies=['127.0.0.1', '10.0.0.0/8'])

        # or register explicitly for more control:
        from blackbull import TrustedProxyMiddleware
        app.use(TrustedProxyMiddleware(['127.0.0.1', '::1']))
    """

    _LOOPBACK: tuple[str, ...] = ('127.0.0.1', '::1')

    def __init__(self, trusted_proxies: list[str] | str | None = None) -> None:
        if trusted_proxies is None:
            trusted_proxies = list(self._LOOPBACK)
        elif isinstance(trusted_proxies, str):
            trusted_proxies = [trusted_proxies]
        self._networks = [ipaddress.ip_network(p, strict=False) for p in trusted_proxies]

    def _is_trusted(self, ip: str) -> bool:
        try:
            addr = ipaddress.ip_address(ip)
        except ValueError:
            return False
        return any(addr in net for net in self._networks)

    async def __call__(self, scope, receive, send, call_next) -> None:
        # HTTP arrives as a native :class:`Connection`; the WebSocket path (still
        # ASGI-scope-shaped) arrives as a scope dict. Read/write the request
        # fields off whichever we were handed.
        is_conn = isinstance(scope, Connection)
        rtype = scope.type if is_conn else scope.get('type')
        if rtype not in ('http', 'websocket'):
            await call_next(scope, receive, send)
            return

        client = scope.client if is_conn else scope.get('client')
        peer_ip = (client or [''])[0]
        if not self._is_trusted(peer_ip):
            await call_next(scope, receive, send)
            return

        headers = scope.headers if is_conn else scope['headers']
        if not isinstance(headers, Headers):
            headers = Headers(headers)

        # Accumulate the rewrites (``_MISS`` = unchanged) so the apply step is a
        # single conn-vs-scope-dict branch.
        new_client = _MISS
        new_scheme = _MISS
        new_root = _MISS

        forwarded = headers.get(b'forwarded', b'').decode()
        if forwarded:
            # RFC 7239 takes precedence over X-Forwarded-*
            parsed = _parse_forwarded(forwarded)
            if 'for' in parsed:
                new_client = [parsed['for'].lstrip('['), 0]
            if 'proto' in parsed:
                new_scheme = parsed['proto']
        else:
            xff = headers.get(b'x-forwarded-for', b'').decode()
            if xff:
                # Walk left-to-right; first non-trusted entry is the real client
                for candidate in (h.strip() for h in xff.split(',')):
                    if not self._is_trusted(candidate):
                        new_client = [candidate, 0]
                        break

            xfp = headers.get(b'x-forwarded-proto', b'').decode()
            if xfp:
                new_scheme = xfp.strip().lower()

        # X-Forwarded-Prefix — the reverse-proxy mount prefix, honoured only
        # here (behind the trusted-peer gate).  The parser layer deliberately
        # ignores it off the wire (bug 1.16); a spoofed prefix from an
        # untrusted client would otherwise poison URL generation / routing.
        xf_prefix = headers.get(b'x-forwarded-prefix', b'').decode()
        if xf_prefix:
            new_root = xf_prefix.rstrip('/')

        if is_conn:
            if new_client is not _MISS:
                scope.client = tuple(new_client) if new_client else None
            if new_scheme is not _MISS:
                scope.scheme = new_scheme
            if new_root is not _MISS:
                scope.root_path = new_root
        else:
            # WebSocket scope dict — mutate it, then mirror onto the stashed
            # Connection when the self-hosted actor provided one (``TrustedProxy``
            # is the only in-tree request mutator, §9 risk table).
            if new_client is not _MISS:
                scope['client'] = new_client
            if new_scheme is not _MISS:
                scope['scheme'] = new_scheme
            if new_root is not _MISS:
                scope['root_path'] = new_root
            conn = scope.get(CONNECTION_STASH_KEY)
            if conn is not None:
                if new_client is not _MISS:
                    conn.client = tuple(new_client) if new_client else None
                if new_scheme is not _MISS:
                    conn.scheme = new_scheme
                if new_root is not _MISS:
                    conn.root_path = new_root

        await call_next(scope, receive, send)
