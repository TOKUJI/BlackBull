"""Response caching middleware (RFC 9111 — HTTP Caching).

Caches successful GET/HEAD responses in a per-worker, in-memory LRU and
replays them without running the handler.  It reads ``Cache-Control`` in both
directions, generates a weak ETag when the handler supplies none, answers
``If-None-Match`` with a 304, and keys variants by ``Vary``.  There is no
invalidation API and nothing is shared between workers: restart the worker, or
wait out the lifetime.  ``docs/guide/middleware.md`` tabulates the constructor.
"""
from __future__ import annotations

import hashlib
import time
from collections import OrderedDict
from collections.abc import Iterable, Iterator
from datetime import timezone
from email.utils import parsedate_to_datetime
from typing import NamedTuple
from urllib.parse import urlsplit

from ..connection import Connection
from ..headers import Headers
from ..native import NativeResponse
from ..protocol.field_grammar import FIELD_VALUE_ALLOWED_SET, TCHAR_SET
from .utils import as_middleware

#: Narrower than RFC 9110 §15's heuristically cacheable set: caching an error
#: is rarely what an application meant.
_DEFAULT_CACHEABLE_STATUSES = frozenset({200, 203, 300, 301, 308, 404, 410, 414, 451})

_DEFAULT_CACHEABLE_METHODS = frozenset({'GET', 'HEAD'})

#: Clamp on a stated lifetime, so a parseable-but-absurd ``max-age`` cannot
#: overflow an entry's expiry (RFC 9111 §4.2.1).  Clamping shortens, never
#: widens.
_MAX_DELTA_SECONDS = 365 * 24 * 60 * 60

#: Cap on the variants one base key holds, so a peer varying an ``Accept-*``
#: header cannot grow one bucket without bound.
_MAX_VARIANTS_PER_KEY = 16




class _StoredResponse(NamedTuple):
    """A cached response as data, so every replay builds a fresh message.

    A ready-made object cannot be handed out twice: middleware below this one
    append to a response's header list in place, growing the entry per hit.
    """
    status: int
    header: list[tuple[bytes, bytes]]
    body: bytes
    etag: bytes
    expires_at: float
    stored_at: float

    def replay(self, age: int) -> NativeResponse:
        """A private copy carrying its *current* age (RFC 9111 §4.2.3)."""
        header = [(name, value) for name, value in self.header
                  if name.lower() != b'age']
        header.append((b'age', str(age).encode()))
        return NativeResponse(status=self.status, header=header, body=self.body)

    def expired(self, now: float | None = None) -> bool:
        return (now if now is not None else time.monotonic()) >= self.expires_at

    def age(self, now: float | None = None) -> float:
        """Seconds since the origin generated the response: ``stored_at`` is
        back-dated by the arriving ``Age``, so this is the whole age, not the
        residency (§4.2.3)."""
        return (now if now is not None else time.monotonic()) - self.stored_at


class _Variants:
    """Everything cached for one method, origin, path and query string.

    The ``Vary`` field names sit beside the entries they key, so they cannot be
    evicted ahead of them — which a second LRU over the names would allow.
    """
    __slots__ = ('vary_fields', 'entries')

    def __init__(self, vary_fields: tuple[bytes, ...] = ()):
        self.vary_fields = vary_fields
        self.entries: OrderedDict[tuple, _StoredResponse] = OrderedDict()


@as_middleware
class Cache:
    """Per-worker in-memory response cache."""

    def __init__(
        self,
        max_age: int = 300,
        *,
        max_entries: int = 1024,
        cacheable_methods: frozenset[str] | set[str] | tuple[str, ...] = _DEFAULT_CACHEABLE_METHODS,
        cacheable_statuses: frozenset[int] | set[int] | tuple[int, ...] = _DEFAULT_CACHEABLE_STATUSES,
        cache_authenticated: bool = False,
        generate_etag: bool = True,
    ):
        if max_age <= 0:
            raise ValueError(f'max_age must be > 0; got {max_age}')
        if max_entries <= 0:
            raise ValueError(f'max_entries must be > 0; got {max_entries}')
        self._max_age = max_age
        self._max_entries = max_entries
        self._cacheable_methods = frozenset(cacheable_methods)
        self._cacheable_statuses = frozenset(cacheable_statuses)
        self._cache_authenticated = cache_authenticated
        self._generate_etag = generate_etag
        # Base key → variants.  OrderedDict gives O(1) move-to-end and
        # ``popitem(last=False)`` for the LRU, as functools.lru_cache does.
        self._store: OrderedDict[tuple, _Variants] = OrderedDict()

    async def __call__(self, conn, receive, send, call_next):
        # The app converts an external host's scope once, on the way in, so
        # every middleware sees a native Connection: the only requests to
        # decline here are the non-HTTP ones.
        if conn.type != 'http' or conn.method not in self._cacheable_methods:
            await call_next(conn, receive, send)
            return
        if not self._cache_authenticated and b'authorization' in conn.headers:
            # RFC 9111 §3.5 — shared only when the application opted in.
            await call_next(conn, receive, send)
            return

        cc = conn.headers.get_combined(b'cache-control')
        pragma = conn.headers.get_combined(b'pragma')
        if _must_not_store(cc):
            await call_next(conn, receive, send)
            return

        origin = _origin(conn)
        if origin is None:
            # No unambiguous origin: bypass rather than share a bucket.
            await call_next(conn, receive, send)
            return
        base_key = (conn.method, origin, conn.path, conn.query_string)
        variants = self._store.get(base_key)
        variant_key = (_variant_key(variants.vary_fields, conn.headers)
                       if variants else ())
        entry = variants.entries.get(variant_key) if variants else None

        # --- cache hit? ---
        if (entry is not None and not entry.expired()
                and _may_reuse(entry, cc, pragma)):
            self._store.move_to_end(base_key)
            variants.entries.move_to_end(variant_key)
            age = max(0, int(entry.age()))
            inm = conn.headers.get(b'if-none-match')
            if inm and _etag_matches(inm, entry.etag):
                await send(NativeResponse(
                    status=304,
                    header=[(b'etag', entry.etag),
                            (b'age', str(age).encode())],
                    body=b''))
                return
            await send(entry.replay(age))
            return

        # --- cache miss: hand the response to a capture, which decides ---
        capture = _Capture(self, conn, send, base_key)
        await call_next(conn, receive, capture.send)
        await capture.release()         # a handler that never sent a body

    def _storable(self, status: int | None,
                  headers: list[tuple[bytes, bytes]]) -> bool:
        """RFC 9111 §3 / §5.2.2 — whether this response may be stored."""
        if status not in self._cacheable_statuses:
            return False
        if {name for name, _ in _directives(headers)} & {
                b'no-store', b'private', b'no-cache'}:
            return False
        # A field nobody can read could be stating one of those.
        return all(_readable(value) for name, value in headers
                   if name.lower() == b'cache-control')

    def _remember(self, base_key: tuple, req_headers: Headers,
                  vary_fields: tuple[bytes, ...], status: int,
                  headers: list[tuple[bytes, bytes]], body: bytes,
                  etag: bytes) -> None:
        """Store one response, evicting whatever makes room for it."""
        stated = _stated_freshness(headers)
        # Only a response that states no usable lifetime takes the default.
        ttl = self._max_age if stated is None else stated
        now = time.monotonic() - _incoming_age(headers)
        bucket = self._store.get(base_key)
        if bucket is None:
            bucket = _Variants(vary_fields)
            self._store[base_key] = bucket
        elif bucket.vary_fields != vary_fields:
            # Keys built from the old Vary field names are unreachable now.
            bucket.vary_fields = vary_fields
            bucket.entries.clear()
        key = _variant_key(vary_fields, req_headers)
        bucket.entries[key] = _StoredResponse(
            status=status, header=list(headers), body=body, etag=etag,
            expires_at=now + ttl, stored_at=now)
        bucket.entries.move_to_end(key)
        self._store.move_to_end(base_key)
        while len(bucket.entries) > _MAX_VARIANTS_PER_KEY:
            bucket.entries.popitem(last=False)
        while len(self._store) > self._max_entries:
            self._store.popitem(last=False)

    def _etag(self, body: bytes) -> bytes:
        # Weak: what is hashed is the body, while a variant served to another
        # client may differ in encoding or negotiation.
        return b'W/"' + hashlib.sha256(body).hexdigest()[:16].encode() + b'"'


class _Capture:
    """The response of one cache miss, held until its body is known.

    Whether a response can be stored is only settled once its body has
    arrived: a generated ETag belongs in the header arm *before* the first
    byte leaves, and a response that never completes must still reach the
    client in order.  Keeping that state here leaves
    [`Cache.__call__`][blackbull.middleware.cache.Cache] the hit/miss decision
    alone.
    """
    __slots__ = ('_cache', '_conn', '_send', '_base_key', '_held', '_body',
                 '_status', '_headers', '_vary_fields', '_released')

    def __init__(self, cache: 'Cache', conn: Connection, send,
                 base_key: tuple):
        self._cache = cache
        self._conn = conn
        self._send = send
        self._base_key = base_key
        self._held: list[NativeResponse] = []
        self._body = bytearray()
        self._status: int | None = None
        self._headers: list[tuple[bytes, bytes]] = []
        self._vary_fields: tuple[bytes, ...] | None = ()
        self._released = False

    async def release(self) -> None:
        """Send everything held, once, in the order it arrived."""
        if not self._released:
            for buffered in self._held:
                await self._send(buffered)
            self._held.clear()
            self._released = True

    async def _forward(self, event) -> None:
        """Give up on storing this response, keeping the events in order."""
        self._held.append(event)
        await self.release()

    async def send(self, event) -> None:
        """Take one response event, holding it while it may still be stored."""
        if self._released:
            await self._send(event)
            return
        if (not isinstance(event, NativeResponse)
                or event.file_path is not None
                or event.expects_trailers
                or event.trailers is not None):
            # Nothing about this shape can be stored, and the rest of the
            # response has to follow it out unchanged.
            await self._forward(event)
            return
        if event._header is not None:
            self._status = event.status
            self._headers = list(event._header)
            self._vary_fields = _vary_fields(self._headers)
            # ``Vary: *`` and an unstorable status or directive are settled by
            # the header alone: stop holding the body as well.
            if (self._vary_fields is None
                    or not self._cache._storable(self._status, self._headers)):
                await self._forward(event)
                return
        if event._body is None:
            self._held.append(event)                # the header arm alone
            return
        if event.more_body or self._status is None:
            await self._forward(event)
            return

        self._body.extend(event._body)
        self._held.append(event)
        etag = _response_etag(self._headers)
        if etag is None and self._cache._generate_etag:
            etag = self._cache._etag(bytes(self._body))
            # The live response and the stored copy must agree on it.
            self._headers.append((b'etag', etag))
            for buffered in self._held:
                if buffered._header is not None:
                    buffered.header = self._headers
                    break
        if etag is not None:
            self._cache._remember(self._base_key, self._conn.headers,
                                  self._vary_fields, self._status,
                                  self._headers, bytes(self._body), etag)
        await self.release()


# --- header inspection helpers ---------------------------------------------

def _origin(conn: Connection) -> tuple[str, str, int] | None:
    """Effective HTTP origin, after trusted middleware has applied rewrites.

    Native HTTP/2 maps :authority into Host before dispatch, as does the ASGI
    boundary.  Forwarded headers are not authority here: only the configured
    trusted-proxy layer may change what the application sees.
    """
    scheme = conn.scheme.lower()
    default_port = {'http': 80, 'https': 443}.get(scheme)
    if default_port is None:
        return None
    hosts = conn.headers.getlist(b'host')
    if len(hosts) > 1:
        return None
    try:
        if hosts:
            authority = hosts[0][1].strip(b' \t').decode('ascii')
        elif conn.server is not None:
            host, port = conn.server
            # ASGI server tuples use an unbracketed IPv6 address; URI
            # authority syntax needs brackets to distinguish it from a port.
            authority = (f'[{host}]'
                         if ':' in host and not host.startswith('[') else host)
            if port is not None:
                authority += f':{port}'
        else:
            return None
        # ``urlsplit`` removes some control characters and interprets
        # delimiters; do not let those transformations alias an ambiguous value
        # to a cacheable origin.  Request validation belongs to the protocol
        # layer, so this is only a second reading of the same value.
        if not authority or any(ord(c) <= 32 or ord(c) == 127 or c in '/?#@\\'
                                for c in authority):
            return None
        literal = authority.startswith('[')
        if literal:
            end = authority.find(']')
            suffix = authority[end + 1:]
            if end < 0 or (suffix and not suffix.startswith(':')):
                return None
        parts = urlsplit('//' + authority)
        host = parts.hostname
        port = parts.port
        if not host:
            return None
    except (UnicodeError, ValueError):
        return None
    # RFC 9110 §4.3.1: host/scheme case and explicit default ports do not
    # identify different origins, and integer conversion normalizes leading
    # zeros.  Preserve IP-literal syntax: [v1.example] (IPvFuture) is not the
    # registered name v1.example.  urlsplit lowercases the hostname while
    # preserving the case-sensitive zone identifier of a scoped address.
    return (scheme, f'[{host}]' if literal else host,
            default_port if port is None else port)


def _must_not_store(cc: bytes | None) -> bool:
    """RFC 9111 §5.2.1.5 — a request ``no-store``, or a field we cannot read.

    A field nobody can read might be stating one, and a wrong reading cannot
    undo the response it stored.
    """
    return cc is not None and (not _readable(cc) or b'no-store' in _names(cc))


def _is_field_content(octet: int) -> bool:
    """RFC 9110 §5.5 field-content — the octets this parser lets a quoted
    directive value hold.  That is what ``quoted-string`` admits between its
    quotes, plus the ``"`` and ``\\`` this parser is deliberately lenient
    about."""
    return octet in FIELD_VALUE_ALLOWED_SET


def _parse_directives(value: bytes) -> list[tuple[bytes, bytes | None]] | None:
    """The ``(name, value)`` directives of one field, or ``None`` if unreadable.

    RFC 9111 §5.2 — ``#cache-directive``, a directive being
    ``token [ "=" ( token / quoted-string ) ]`` — walked on the raw bytes,
    because ``parse_http_list`` drops the backslash of a quoted-pair and its
    pieces cannot tell an escaped quote from the one that closes a value.
    Names come back lowercased, as the RFC compares them.  A field that breaks
    the grammar is unreadable and its callers act on nothing in it.
    """
    pairs: list[tuple[bytes, bytes | None]] = []
    pos = 0
    while True:
        # OWS sits around a comma and around the whole list, never elsewhere.
        while pos < len(value) and value[pos] in (0x20, 0x09):
            pos += 1
        if pos == len(value):
            return pairs
        if value[pos] == 0x2C:
            pos += 1                    # an empty member is ignored (§5.6.1.1)
            continue
        start = pos
        while pos < len(value) and value[pos] in TCHAR_SET:
            pos += 1
        name = value[start:pos].lower()
        if not name:
            return None                 # a quote or comma where a name belongs
        if pos < len(value) and value[pos] == 0x3D:
            pos += 1
            if pos < len(value) and value[pos] == 0x22:
                pos += 1
                out = bytearray()
                while True:
                    if pos == len(value):
                        return None                     # unterminated string
                    octet = value[pos]
                    pos += 1
                    if octet == 0x22:
                        break                           # the closing quote
                    if octet == 0x5C:
                        if pos == len(value):
                            return None                 # nothing to pair with
                        octet = value[pos]
                        pos += 1
                    if not _is_field_content(octet):
                        return None                     # not a quoted-string
                    out += bytes((octet,))
                pairs.append((name, bytes(out)))
            else:
                start = pos
                while pos < len(value) and value[pos] in TCHAR_SET:
                    pos += 1
                if pos == start:
                    return None             # '=' with no value
                pairs.append((name, value[start:pos]))
        else:
            pairs.append((name, None))
        while pos < len(value) and value[pos] in (0x20, 0x09):
            pos += 1
        if pos == len(value):
            return pairs
        if value[pos] != 0x2C:
            return None                     # a directive ends at a comma
        pos += 1


def _names(value: bytes) -> list[bytes]:
    """The directive names of one field, or none if it is unreadable."""
    pairs = _parse_directives(value)
    return [name for name, _ in pairs] if pairs else []


def _readable(value: bytes) -> bool:
    """Whether one field parses as directives."""
    return _parse_directives(value) is not None


def _directives(fields: Iterable[tuple[bytes, bytes]]
                ) -> Iterator[tuple[bytes, bytes | None]]:
    """The directives of every ``Cache-Control`` field, in order.

    A field that cannot be read contributes nothing, so a caller that must not
    act on a partly-read field checks [`_readable`][] itself.
    """
    for name, value in fields:
        if name.lower() == b'cache-control':
            yield from _parse_directives(value) or ()


def _smallest(fields: Iterable[tuple[bytes, bytes]],
              name: bytes) -> int | None:
    """The smallest value these fields state for *name*, or ``None``.

    Repeated directives resolve to the most restrictive reading (RFC 9111
    §4.2.1 allows the first occurrence or a stale one); a value that is not a
    number leaves it unstated.
    """
    values = []
    for key, raw in _directives(fields):
        if key != name or raw is None:
            continue
        sign, digits = ((raw[:1], raw[1:]) if raw[:1] in (b'-', b'+')
                        else (b'', raw))
        if not digits.isdigit():
            continue
        try:
            values.append(int(raw))
        except ValueError:               # more digits than int() will read
            values.append(-1 if sign == b'-' else _MAX_DELTA_SECONDS)
    return max(0, min(min(values), _MAX_DELTA_SECONDS)) if values else None


def _incoming_age(fields: Iterable[tuple[bytes, bytes]]) -> int:
    """The response's ``Age`` in seconds (RFC 9111 §4.2.3).

    Absent or unreadable reads as 0: an age the origin did not state is not
    evidence of staleness.
    """
    for name, value in fields:
        if name.lower() != b'age':
            continue
        try:
            return max(0, min(int(value.strip()), _MAX_DELTA_SECONDS))
        except ValueError:
            return 0
    return 0


def _date_seconds(value: bytes) -> float | None:
    """An HTTP-date as a POSIX timestamp, or ``None`` if it does not parse."""
    try:
        when = parsedate_to_datetime(value.decode('latin-1'))
    except (TypeError, ValueError):
        return None
    if when is None:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return when.timestamp()


def _expires_in(fields: Iterable[tuple[bytes, bytes]]) -> int | None:
    """Freshness from ``Expires``, or ``None`` when there is no such field.

    RFC 9111 §5.3: the lifetime is ``Expires − Date`` (the wall clock when the
    origin sent no usable ``Date``) and an unparsable value means the past.
    The earliest ``Expires`` against the latest ``Date`` is the conservative
    reading of a repeated field, and this is consulted only when neither
    ``s-maxage`` nor ``max-age`` stated a lifetime.
    """
    expires = [_date_seconds(v) for n, v in fields if n.lower() == b'expires']
    if not expires:
        return None
    if any(when is None for when in expires):
        return 0
    dates = [_date_seconds(v) for n, v in fields if n.lower() == b'date']
    base = max([when for when in dates if when is not None], default=time.time())
    return max(0, min(int(min(expires) - base), _MAX_DELTA_SECONDS))


def _stated_max_age(fields: Iterable[tuple[bytes, bytes]]) -> int | None:
    """``s-maxage`` (which overrides ``max-age``, §5.2.2.10) or ``None``."""
    stated = _smallest(fields, b's-maxage')
    return stated if stated is not None else _smallest(fields, b'max-age')


def _stated_freshness(fields: Iterable[tuple[bytes, bytes]]) -> int | None:
    """The response's own lifetime, or ``None`` when it states none."""
    stated = _stated_max_age(fields)
    return stated if stated is not None else _expires_in(fields)


def _may_reuse(entry: _StoredResponse, cc: bytes | None,
               pragma: bytes | None) -> bool:
    """Whether *entry* may answer this request without validation.

    RFC 9111 §5.2.1 — a request's ``no-cache`` asks the origin to validate, and
    ``max-age=N`` refuses a copy older than N, so ``max-age=0`` always
    validates.  ``Pragma: no-cache`` counts when no ``Cache-Control`` was sent
    (§5.4), and a field that cannot be read could be hiding a ``no-cache``.
    ``min-fresh``, ``max-stale`` and ``only-if-cached`` are not implemented.
    """
    if cc is not None and not _readable(cc):
        return False
    if cc is None and pragma is not None and (
            not _readable(pragma) or b'no-cache' in _names(pragma)):
        return False
    # The name alone: ``no-cache="field"`` still asks for validation, and this
    # cache does not validate per field (§5.2.2.4).
    if cc is not None and b'no-cache' in _names(cc):
        return False
    limit = _smallest([(b'cache-control', cc)], b'max-age') if cc else None
    return limit is None or entry.age() <= limit


def _vary_fields(fields: Iterable[tuple[bytes, bytes]]
                 ) -> tuple[bytes, ...] | None:
    """The response's ``Vary`` field names, lowercased and sorted.

    ``None`` is ``Vary: *``, unstorable by a shared cache (RFC 9110 §12.5.5);
    no ``Vary`` yields ``()``, the key of the one non-varying entry.
    """
    names: set[bytes] = set()
    for name, value in fields:
        if name.lower() != b'vary':
            continue
        for token in value.split(b','):
            token = token.strip().lower()
            if token == b'*':
                return None
            if token:
                names.add(token)
    return tuple(sorted(names))


def _variant_key(vary_fields: tuple[bytes, ...], headers: Headers) -> tuple:
    """The key of one variant: the request's values for those field names."""
    return tuple(headers.get(name, b'') for name in vary_fields)


def _response_etag(fields: Iterable[tuple[bytes, bytes]]) -> bytes | None:
    """The response's own ``ETag``, or ``None``."""
    return next((value for name, value in fields if name.lower() == b'etag'),
                None)


def _etag_matches(if_none_match: bytes, etag: bytes) -> bool:
    """RFC 9110 §13.1.2 — ``If-None-Match`` against one ETag.

    Weak comparison ignores ``W/`` on either side; ``*`` and a list of
    candidates are both read.
    """
    if if_none_match.strip() == b'*':
        return True
    target = etag[2:] if etag.startswith(b'W/') else etag
    for candidate in if_none_match.split(b','):
        candidate = candidate.strip()
        if candidate.startswith(b'W/'):
            candidate = candidate[2:]
        if candidate == target:
            return True
    return False
