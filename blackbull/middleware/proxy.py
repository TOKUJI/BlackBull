"""Recovering the real client from behind a reverse proxy.

Once a proxy sits in front of the server the TCP peer *is* the proxy, and every
request appears to come from it.
[`TrustedProxy`][blackbull.middleware.proxy.TrustedProxy] restores the client
address, scheme and mount prefix from the headers the proxy set — but only when
the peer that set them is one you named as trusted, since any client can send
those headers itself.

Nothing here is honoured off the wire by default: the parser ignores
``X-Forwarded-Prefix``, and client and scheme keep whatever the socket
reported, until this middleware decides the hop is trustworthy.

See ``docs/deployment/behind-reverse-proxy.md`` for the deployment shape.
"""
import ipaddress
import re

from ..connection import CONNECTION_STASH_KEY, Connection
from ..headers import Headers
from ..protocol.field_grammar import FIELD_VALUE_ALLOWED_SET, TCHAR_SET

_MISS = object()


def _parse_forwarded(value: bytes) -> list[dict[bytes, bytes]] | None:
    """Preserve element boundaries; an ambiguous field cannot prove a hop."""
    elements: list[dict[bytes, bytes]] = []
    element: dict[bytes, bytes] = {}
    has_element = False
    pos = 0
    while pos < len(value):
        if value[pos] in (32, 9):
            pos += 1
            continue
        if value[pos] == 44:
            if has_element:
                elements.append(element)
            element = {}
            has_element = False
            pos += 1
            continue
        if value[pos] == 59:
            has_element = True
            pos += 1
            continue
        has_element = True
        start = pos
        while pos < len(value) and value[pos] in TCHAR_SET:
            pos += 1
        name = value[start:pos].lower()
        if not name or name in element or pos == len(value) or value[pos] != 61:
            return None
        pos += 1
        if pos < len(value) and value[pos] == 34:
            pos += 1
            out = bytearray()
            while True:
                if pos == len(value):
                    return None
                octet = value[pos]
                pos += 1
                if octet == 34:
                    break
                if octet == 92:
                    if pos == len(value):
                        return None
                    octet = value[pos]
                    pos += 1
                if octet not in FIELD_VALUE_ALLOWED_SET:
                    return None
                out.append(octet)
            parsed = bytes(out)
        else:
            start = pos
            while pos < len(value) and value[pos] in TCHAR_SET:
                pos += 1
            if pos == start:
                return None
            parsed = value[start:pos]
        element[name] = parsed
        while pos < len(value) and value[pos] in (32, 9):
            pos += 1
        if pos < len(value) and value[pos] not in (44, 59):
            return None
    if has_element:
        elements.append(element)
    return elements


def _node_ip(value: bytes, *, forwarded: bool = False) -> str | None:
    try:
        text = value.decode('ascii')
        if '%' in text:
            return None
        if forwarded:
            port = None
            if text.startswith('['):
                host, separator, tail = text[1:].partition(']')
                if not separator or (tail and not tail.startswith(':')):
                    return None
                if ipaddress.ip_address(host).version != 6:
                    return None
                port = tail[1:] if tail else None
            else:
                host, separator, tail = text.partition(':')
                if ipaddress.ip_address(host).version != 4:
                    return None
                port = tail if separator else None
            if port is not None and not (
                re.fullmatch(r'_[A-Za-z0-9._-]+', port)
                or (port.isascii() and port.isdecimal() and len(port) <= 5
                    and int(port) <= 65535)
            ):
                return None
            text = host
        return str(ipaddress.ip_address(text))
    except (ValueError, UnicodeError):
        return None


def _scheme(value: bytes | None) -> str | None:
    if value and re.fullmatch(rb'[A-Za-z][A-Za-z0-9+.-]*', value):
        return value.decode('ascii').lower()
    return None


def _singleton(headers: Headers, name: bytes) -> bytes:
    fields = headers.getlist(name)
    if len(fields) != 1 or b',' in fields[0][1]:
        return b''
    return fields[0][1].strip(b' \t')


def _prefix(value: bytes) -> str | None:
    if (not value.startswith(b'/') or value.startswith(b'//')
            or any(c < 33 or c == 127 or c in b'?#\\' for c in value)):
        return None
    try:
        prefix = value.decode('utf-8')
        if not prefix.isprintable() or any(c.isspace() for c in prefix):
            return None
        return prefix.rstrip('/')
    except UnicodeError:
        return None


class TrustedProxy:
    """Rewrite ``conn['client']`` and ``conn['scheme']`` from proxy headers.

    Applied only when the direct TCP peer matches the configured trusted set.
    Trusted proxies must append their observed peer or overwrite the chain;
    standalone proto/prefix assertions must replace client-supplied values.

    Supported headers (in precedence order):

    1. RFC 7239 ``Forwarded`` — ``for=<ip>; proto=<scheme>``
    2. ``X-Forwarded-For`` — walk right to left to the first untrusted IP
    3. ``X-Forwarded-Proto`` — rewrite ``conn['scheme']``

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

    async def __call__(self, conn, receive, send, call_next) -> None:
        # HTTP and WebSocket both arrive as a native [`Connection`][]; the
        # dict branch is defensive against a raw ASGI scope dict (only reachable
        # outside BlackBull's own dispatch). Read/write off whichever we got.
        is_conn = isinstance(conn, Connection)
        rtype = conn.type if is_conn else conn.get('type')
        if rtype not in ('http', 'websocket'):
            await call_next(conn, receive, send)
            return

        client = conn.client if is_conn else conn.get('client')
        peer_ip = (client or [''])[0]
        if not self._is_trusted(peer_ip):
            await call_next(conn, receive, send)
            return

        headers = conn.headers if is_conn else conn['headers']
        if not isinstance(headers, Headers):
            headers = Headers(headers)

        # Accumulate the rewrites (``_MISS`` = unchanged) so the apply step is a
        # single conn-vs-conn-dict branch.
        new_client = _MISS
        new_scheme = _MISS
        new_root = _MISS

        if b'forwarded' in headers:
            forwarded = b','.join(v for _, v in headers.getlist(b'forwarded'))
            elements = _parse_forwarded(forwarded)
            if elements:
                for element in reversed(elements):
                    candidate = _node_ip(element.get(b'for', b''), forwarded=True)
                    if candidate is None:
                        new_scheme = _MISS
                        break
                    new_client = [candidate, 0]
                    scheme = _scheme(element.get(b'proto'))
                    new_scheme = scheme if scheme is not None else _MISS
                    if not self._is_trusted(candidate):
                        break
        else:
            fields = headers.getlist(b'x-forwarded-for')
            if fields:
                for value in reversed(b','.join(v for _, v in fields).split(b',')):
                    candidate = _node_ip(value.strip(b' \t'))
                    if candidate is None:
                        break
                    new_client = [candidate, 0]
                    if not self._is_trusted(candidate):
                        break
            scheme = _scheme(_singleton(headers, b'x-forwarded-proto'))
            if scheme is not None:
                new_scheme = scheme

        # Standalone assertions have no standardized correspondence with XFF.
        # The trusted direct proxy must overwrite client-supplied values.
        prefix = _prefix(_singleton(headers, b'x-forwarded-prefix'))
        if prefix is not None:
            new_root = prefix

        if is_conn:
            if new_client is not _MISS:
                conn.client = tuple(new_client) if new_client else None
            if new_scheme is not _MISS:
                conn.scheme = new_scheme
            if new_root is not _MISS:
                conn.root_path = new_root
        else:
            # WebSocket scope dict — mutate it, then mirror onto the stashed
            # Connection when the self-hosted actor provided one (``TrustedProxy``
            # is the only in-tree request mutator, §9 risk table).
            if new_client is not _MISS:
                conn['client'] = new_client
            if new_scheme is not _MISS:
                conn['scheme'] = new_scheme
            if new_root is not _MISS:
                conn['root_path'] = new_root
            stashed = conn.get(CONNECTION_STASH_KEY)
            if stashed is not None:
                if new_client is not _MISS:
                    stashed.client = tuple(new_client) if new_client else None
                if new_scheme is not _MISS:
                    stashed.scheme = new_scheme
                if new_root is not _MISS:
                    stashed.root_path = new_root

        await call_next(conn, receive, send)
