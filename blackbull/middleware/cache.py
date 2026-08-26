"""Response caching middleware (RFC 9111 — HTTP Caching).

Caches successful GET/HEAD responses in a per-worker, in-memory LRU.
Subsequent matching requests are served directly from the cache without
running the handler.  Supports:

* **TTL** — server-side ``max_age`` (default 300 s), overridable by the
  response's ``Cache-Control: max-age=…`` directive (or, when present,
  ``s-maxage=…`` which takes precedence for shared caches).
* **ETag** — auto-generated as ``W/"<sha256-prefix>"`` over the response
  body when the application does not supply one.  The client's
  ``If-None-Match`` header is honoured: a match yields a 304 Not
  Modified with no body, regardless of the cached entry's TTL.
* **Cache-Control respect** — responses carrying ``no-store``, ``private``,
  or ``no-cache`` are passed through and not stored.  Requests carrying
  ``no-store`` skip the cache lookup too.
* **Authorization header** — by default, requests with an
  ``Authorization`` header are NOT served from cache and their
  responses are NOT stored (RFC 9111 §3.5).  Override with
  ``cache_authenticated=True``.

Variant-aware: the response ``Vary`` header is honoured (RFC 9110
§12.5.5).  When a stored response carries e.g. ``Vary: Accept-Encoding``,
the varied request-header values are folded into the cache key so a
brotli variant is never replayed to an ``identity`` client.  A response
with ``Vary: *`` is passed through and not stored.

What it doesn't do (yet):

* No server-side invalidation API.  Restart the worker (or wait for
  TTL) to clear.
* No cross-worker sharing.  The cache is per-process — each worker has
  its own.  Documented limitation.

The store is keyed by ``(method, path, query_string)`` → a per-URL bucket
that holds the response's ``Vary`` field names alongside its variant entries
(one per distinct set of ``(field, request-value)`` pairs named by ``Vary``).
Keeping the vary fields inside the bucket means they can never be evicted
independently of the entries they key (which the earlier two-LRU design
allowed — orphaning the entries).

Usage::

    from blackbull.middleware import Cache

    app.use(Cache(max_age=600))     # 10-minute TTL

    @app.route(path='/feed')
    async def feed(conn, receive, send):
        ...   # served from cache for 10 min after first hit
"""
from __future__ import annotations

import hashlib
import logging
import time
from collections import OrderedDict

from ..connection import Connection
from ..native import NativeResponse
from .utils import as_middleware

logger = logging.getLogger(__name__)


# RFC 9110 §15.x — status codes that are heuristically cacheable.
# We're stricter than the RFC's full list: caching error responses is
# rarely what the user means.
_DEFAULT_CACHEABLE_STATUSES = frozenset({200, 203, 300, 301, 308, 404, 410, 414, 451})

_DEFAULT_CACHEABLE_METHODS = frozenset({'GET', 'HEAD'})


class _Entry:
    """One stored cache hit — the response as *data*, not as a message.

    Held as ``(status, header, body)`` rather than a ready-made
    :class:`~blackbull.native.NativeResponse` so every replay can build a
    fresh object over a fresh header list.  Middleware below the cache — CORS,
    the route header injector — append to ``_header`` **in place**; handing
    out one shared object would grow the stored entry on every hit.
    """
    __slots__ = ('status', 'header', 'body', 'etag', 'expires_at')

    def __init__(self, status: int, header: list[tuple[bytes, bytes]],
                 body: bytes, etag: bytes, expires_at: float):
        self.status = status
        self.header = header
        self.body = body
        self.etag = etag
        self.expires_at = expires_at

    def replay(self) -> 'NativeResponse':
        """A private copy of the stored response, safe to mutate downstream."""
        return NativeResponse(status=self.status, header=list(self.header),
                              body=self.body)

    def expired(self, now: float | None = None) -> bool:
        return (now if now is not None else time.monotonic()) >= self.expires_at


# Safety cap on the number of stored variants for a single base key, so a
# hostile peer varying an Accept-* header cannot grow one bucket without bound.
# Far above any real Accept-Encoding × Accept-Language cross-product.
_MAX_VARIANTS_PER_KEY = 16


class _Bucket:
    """All cached variants for one base key ``(method, path, query_string)``.

    The response ``Vary`` field names live *inside* the bucket, beside the
    per-variant entries — not in a separate LRU.  That is the fix for 1.21g:
    with two independent LRUs (the old ``_store`` + ``_vary_registry``) the vary
    record could be evicted before its entries, orphaning them (future lookups
    rebuilt the variant key with empty vary fields and never matched). Here the
    vary fields cannot outlive their entries, so no orphan is possible.

    ``entries`` is a per-variant LRU keyed by the variant tuple from
    :func:`_vary_key` (``()`` for a non-varying response).
    """
    __slots__ = ('vary_fields', 'entries')

    def __init__(self, vary_fields: tuple[bytes, ...] = ()):
        self.vary_fields = vary_fields
        self.entries: OrderedDict[tuple, _Entry] = OrderedDict()


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
        # base_key → _Bucket.  OrderedDict gives O(1) move-to-end on access +
        # popitem(last=False) for LRU eviction — same pattern as
        # :func:`functools.lru_cache`.  ``_max_entries`` bounds the number of
        # distinct URLs (base keys); each bucket LRU-bounds its own variants.
        self._store: OrderedDict[tuple, _Bucket] = OrderedDict()

    # ---- ASGI surface ----------------------------------------------------

    async def __call__(self, conn, receive, send, call_next):
        # Native Connection for HTTP and WebSocket; the guard is defensive
        # against a raw ASGI scope dict (only reachable outside BlackBull's own
        # dispatch).
        if not isinstance(conn, Connection):
            await call_next(conn, receive, send)
            return

        method = conn.method
        if method not in self._cacheable_methods:
            await call_next(conn, receive, send)
            return

        req_headers = _request_headers(conn)
        if not self._cache_authenticated and b'authorization' in req_headers:
            # RFC 9111 §3.5 — caches MUST NOT use responses to requests with
            # Authorization unless explicit cache-control allows it.
            await call_next(conn, receive, send)
            return
        if _request_has_no_store(req_headers):
            await call_next(conn, receive, send)
            return

        base_key = (method, conn.path, conn.query_string)
        # Look up the bucket for this URL, then the specific variant inside it
        # using the Vary fields recorded on the bucket (empty tuple ⇒ the single
        # non-varying entry keyed by ``()``).
        bucket = self._store.get(base_key)
        variant_key = _vary_key(bucket.vary_fields, req_headers) if bucket else ()
        entry = bucket.entries.get(variant_key) if bucket else None

        # --- cache hit? ---
        if entry is not None and not entry.expired():
            self._store.move_to_end(base_key)      # URL touched → MRU
            bucket.entries.move_to_end(variant_key)  # variant touched → MRU
            inm = req_headers.get(b'if-none-match')
            if inm is not None and _etag_matches(inm, entry.etag):
                await send(NativeResponse(status=304,
                                          header=[(b'etag', entry.etag)],
                                          body=b''))
                return
            # Replay a private copy — downstream middleware append headers in
            # place, and the stored entry must not accumulate them.
            await send(entry.replay())
            return

        # --- cache miss → call inner, buffer, then send + maybe store ---
        # We buffer the response (rather than passing each event straight
        # through) so we can inject an ETag into the *start* event before
        # any bytes hit the client.  For streaming responses (more_body
        # arriving as True on the first body chunk) the buffer drops
        # straight through and we skip caching: a streaming body's size
        # is unknown and hashing it post-hoc would defeat the streaming.
        held: list = []                 # native objects buffered, in order
        body_chunks: list[bytes] = []
        status: int | None = None
        response_headers: list[tuple[bytes, bytes]] = []
        streaming = False
        flushed = False

        async def cap_send(event):
            """Buffer the response so an ETag can be injected before any byte
            reaches the client.  The seam is native, so the header and body
            arms are read off the object directly — no expansion, no dicts.

            A ``NativeResponse`` may carry the header and terminal body
            together (the complete shape), the header alone, or a body chunk
            alone; all three are handled here.
            """
            nonlocal status, response_headers, streaming, flushed

            if not isinstance(event, NativeResponse):
                # A non-response event (pathsend / push) cannot be cached and
                # cannot be held — release anything buffered, then pass it on.
                if not flushed:
                    streaming = True
                    for buf in held:
                        await send(buf)
                    flushed = True
                await send(event)
                return

            if event._header is not None:
                status = event.status
                response_headers = list(event._header)

            if event.file_path is not None:
                # Sendfile: the bytes never pass through us, so there is
                # nothing to hash and nothing to store.
                streaming = True
                held.append(event)
                if not flushed:
                    for buf in held:
                        await send(buf)
                    flushed = True
                return

            if event._body is None:
                # Header arm alone — hold it for the body that completes it.
                held.append(event)
                return

            if streaming:
                # Already past the switch: every later chunk goes straight
                # out.  Nothing is held and nothing is accumulated, or the
                # stream would be buffered in full to cache a response the
                # docstring says is not cached.
                await send(event)
                return

            if event.more_body:
                # Streaming starts here.  Flush what is held, **clear it**,
                # and switch to pass-through: a streamed body's size is
                # unknown and hashing it post-hoc would defeat the streaming.
                #
                # Clearing is the whole correctness of this arm.  Leaving the
                # buffer populated sent the header and the first chunk a
                # second time when the terminal chunk flushed it again —
                # two ``http.response.start`` events on HTTP/1.1, a duplicated
                # body on HTTP/2.
                streaming = True
                held.append(event)
                for buf in held:
                    await send(buf)
                held.clear()
                flushed = True
                return

            body_chunks.append(event._body)
            # Final body chunk arrived; decide cacheability + ETag now.
            held.append(event)
            body = b''.join(body_chunks)
            if self._should_cache(status, response_headers):
                vary_fields = _response_vary(response_headers)
                etag = _read_etag(response_headers) or (
                    self._make_etag(body) if self._generate_etag else None)
                if etag is not None and _read_etag(response_headers) is None:
                    # Inject the generated ETag before anything is sent, so the
                    # live response and the cached copy carry the same header.
                    # ``response_headers`` is already our own list; the header
                    # arm is updated from it so both agree.
                    response_headers.append((b'etag', etag))
                    for buf in held:
                        if buf._header is not None:
                            buf.header = list(response_headers)
                            break
                # ``vary_fields is None`` ⇒ ``Vary: *`` ⇒ uncacheable.
                if etag is not None and vary_fields is not None:
                    ttl = _response_max_age(response_headers) or self._max_age
                    bucket = self._store.get(base_key)
                    if bucket is None:
                        bucket = _Bucket(vary_fields)
                        self._store[base_key] = bucket
                    elif bucket.vary_fields != vary_fields:
                        # The response's Vary changed; the old variant keys
                        # were built from the old fields and can no longer be
                        # reached — adopt the new fields and drop them.
                        bucket.vary_fields = vary_fields
                        bucket.entries.clear()
                    variant_key = _vary_key(vary_fields, req_headers)
                    # Stored as data, with its own header list: replays build a
                    # fresh object so downstream in-place appends cannot reach
                    # the entry.
                    bucket.entries[variant_key] = _Entry(
                        status=status if status is not None else 200,
                        header=list(response_headers),
                        body=body,
                        etag=etag,
                        expires_at=time.monotonic() + ttl,
                    )
                    bucket.entries.move_to_end(variant_key)
                    self._store.move_to_end(base_key)
                    # Per-bucket variant bound, then per-URL bound.
                    while len(bucket.entries) > _MAX_VARIANTS_PER_KEY:
                        bucket.entries.popitem(last=False)
                    while len(self._store) > self._max_entries:
                        self._store.popitem(last=False)
            # Flush to client.
            for buf in held:
                await send(buf)
            flushed = True

        await call_next(conn, receive, cap_send)

        # If the handler never emitted a terminal body the response was never
        # flushed — forward whatever we have so the client at least sees
        # something.  Pathological case; not cached.
        if not flushed:
            for buf in held:
                await send(buf)

    # ---- helpers --------------------------------------------------------

    def _should_cache(self, status: int | None,
                      headers: list[tuple[bytes, bytes]]) -> bool:
        if status not in self._cacheable_statuses:
            return False
        # RFC 9111 §5.2.2 — these directives forbid storing.
        cc = _cache_control(headers)
        if b'no-store' in cc or b'private' in cc or b'no-cache' in cc:
            return False
        return True

    def _make_etag(self, body: bytes) -> bytes:
        # Weak ETag (W/ prefix) over a sha256 prefix.  Weak because the
        # body bytes are what we hashed but other facets (compression,
        # negotiation) may differ between served variants.
        h = hashlib.sha256(body).hexdigest()[:16]
        return b'W/"' + h.encode() + b'"'


# ---------------------------------------------------------------------------
# Header inspection helpers (kept module-level so the middleware class stays
# focused on the orchestration logic).
# ---------------------------------------------------------------------------

def _request_headers(conn) -> dict[bytes, bytes]:
    """Index the request headers by lowercase name → value (first occurrence)."""
    out: dict[bytes, bytes] = {}
    for name, value in conn.headers:
        n = name.lower()
        if n not in out:
            out[n] = value
    return out


def _request_has_no_store(headers: dict[bytes, bytes]) -> bool:
    """RFC 9111 §5.2.1 — request directive ``Cache-Control: no-store``."""
    cc = headers.get(b'cache-control', b'').lower()
    return any(piece.strip() == b'no-store' for piece in cc.split(b','))


def _cache_control(headers: list[tuple[bytes, bytes]]) -> set[bytes]:
    """Return the set of directive tokens from the response Cache-Control header.

    Values are normalised to lowercase, with leading parameter names only —
    ``max-age=120`` ⇒ ``b'max-age=120'`` is kept whole; ``no-store`` is also
    kept whole.  Callers compare with ``in``.
    """
    tokens: set[bytes] = set()
    for name, value in headers:
        if name.lower() != b'cache-control':
            continue
        for piece in value.split(b','):
            t = piece.strip().lower()
            if t:
                tokens.add(t)
                # Also add the bare directive name so ``'max-age' in cc``
                # works regardless of the value.
                if b'=' in t:
                    tokens.add(t.split(b'=', 1)[0])
    return tokens


def _response_max_age(headers: list[tuple[bytes, bytes]]) -> int | None:
    """Pull ``max-age`` / ``s-maxage`` (preferred) out of Cache-Control."""
    s_max: int | None = None
    max_age: int | None = None
    for name, value in headers:
        if name.lower() != b'cache-control':
            continue
        for piece in value.split(b','):
            t = piece.strip().lower()
            if t.startswith(b's-maxage='):
                try:
                    s_max = int(t[9:])
                except ValueError:
                    pass  # malformed s-maxage → ignore this directive.
            elif t.startswith(b'max-age='):
                try:
                    max_age = int(t[8:])
                except ValueError:
                    pass  # malformed max-age → ignore this directive.
    return s_max if s_max is not None else max_age


def _response_vary(headers: list[tuple[bytes, bytes]]) -> tuple[bytes, ...] | None:
    """Return the response's ``Vary`` field names, lowercased and sorted.

    ``None`` signals ``Vary: *`` (RFC 9110 §12.5.5 — response is
    unstorable by a shared cache).  An absent ``Vary`` yields the empty
    tuple (store under the bare base key).
    """
    fields: set[bytes] = set()
    for name, value in headers:
        if name.lower() != b'vary':
            continue
        for piece in value.split(b','):
            t = piece.strip().lower()
            if t == b'*':
                return None
            if t:
                fields.add(t)
    return tuple(sorted(fields))


def _vary_key(vary_fields: tuple[bytes, ...],
              req_headers: dict[bytes, bytes]) -> tuple:
    """Build the variant portion of the cache key from the request headers
    named by *vary_fields*.  A missing request header contributes ``b''``."""
    return tuple((f, req_headers.get(f, b'')) for f in vary_fields)


def _read_etag(headers: list[tuple[bytes, bytes]]) -> bytes | None:
    for name, value in headers:
        if name.lower() == b'etag':
            return value
    return None


def _etag_matches(if_none_match: bytes, etag: bytes) -> bool:
    """RFC 9110 §13.1.2 — If-None-Match.  ``*`` matches anything; otherwise
    we do a weak comparison (W/ prefix on either side is fine)."""
    inm = if_none_match.strip()
    if inm == b'*':
        return True
    # Multiple ETags separated by commas.
    candidates = [c.strip() for c in inm.split(b',')]
    # Strip the optional weak prefix for weak comparison.
    target = etag
    if target.startswith(b'W/'):
        target = target[2:]
    for cand in candidates:
        c = cand[2:] if cand.startswith(b'W/') else cand
        if c == target:
            return True
    return False
