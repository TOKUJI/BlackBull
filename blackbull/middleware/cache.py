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

What it doesn't do (yet):

* No ``Vary`` header support beyond identity matching.  A request that
  differs only by ``Accept-Encoding`` or ``Accept-Language`` will get
  the first variant cached.
* No server-side invalidation API.  Restart the worker (or wait for
  TTL) to clear.
* No cross-worker sharing.  The cache is per-process — each worker has
  its own.  Documented limitation.

The cache key is ``(method, path, query_string)``.

Usage::

    from blackbull.middleware import Cache

    app.use(Cache(max_age=600))     # 10-minute TTL

    @app.route(path='/feed')
    async def feed(scope, receive, send):
        ...   # served from cache for 10 min after first hit
"""
from __future__ import annotations

import hashlib
import logging
import time
from collections import OrderedDict
from typing import Any

from ..server.constants import ASGIEvent
from .utils import as_middleware

logger = logging.getLogger(__name__)


# RFC 9110 §15.x — status codes that are heuristically cacheable.
# We're stricter than the RFC's full list: caching error responses is
# rarely what the user means.
_DEFAULT_CACHEABLE_STATUSES = frozenset({200, 203, 300, 301, 308, 404, 410, 414, 451})

_DEFAULT_CACHEABLE_METHODS = frozenset({'GET', 'HEAD'})


class _Entry:
    """One stored cache hit.

    ``events`` is the list of ASGI ``http.response.start`` / ``.body``
    events captured from the handler.  Replay copies the list (so a
    later mutation of e.g. response headers in another middleware on
    the next request doesn't corrupt the cached copy).
    """
    __slots__ = ('events', 'etag', 'expires_at')

    def __init__(self, events: list[dict], etag: bytes, expires_at: float):
        self.events = events
        self.etag = etag
        self.expires_at = expires_at

    def expired(self, now: float | None = None) -> bool:
        return (now if now is not None else time.monotonic()) >= self.expires_at


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
        # OrderedDict gives us O(1) move-to-end on access + popitem(last=False)
        # for LRU eviction — same pattern as :func:`functools.lru_cache`.
        self._store: OrderedDict[tuple, _Entry] = OrderedDict()

    # ---- ASGI surface ----------------------------------------------------

    async def __call__(self, scope, receive, send, call_next):
        if scope.get('type') != 'http':
            await call_next(scope, receive, send)
            return

        method = scope.get('method', '')
        if method not in self._cacheable_methods:
            await call_next(scope, receive, send)
            return

        req_headers = _request_headers(scope)
        if not self._cache_authenticated and b'authorization' in req_headers:
            # RFC 9111 §3.5 — caches MUST NOT use responses to requests with
            # Authorization unless explicit cache-control allows it.
            await call_next(scope, receive, send)
            return
        if _request_has_no_store(req_headers):
            await call_next(scope, receive, send)
            return

        key = (method, scope.get('path', ''), scope.get('query_string', b''))

        # --- cache hit? ---
        entry = self._store.get(key)
        if entry is not None and not entry.expired():
            self._store.move_to_end(key)  # touched → most-recently-used
            inm = req_headers.get(b'if-none-match')
            if inm is not None and _etag_matches(inm, entry.etag):
                await send({'type': ASGIEvent.HTTP_RESPONSE_START, 'status': 304,
                            'headers': [(b'etag', entry.etag)]})
                await send({'type': ASGIEvent.HTTP_RESPONSE_BODY, 'body': b''})
                return
            # Replay the stored events.  We copy each dict so downstream
            # middleware can't mutate our cached state.
            for event in entry.events:
                await send(dict(event))
            return

        # --- cache miss → call inner, buffer, then send + maybe store ---
        # We buffer the response (rather than passing each event straight
        # through) so we can inject an ETag into the *start* event before
        # any bytes hit the client.  For streaming responses (more_body
        # arriving as True on the first body chunk) the buffer drops
        # straight through and we skip caching: a streaming body's size
        # is unknown and hashing it post-hoc would defeat the streaming.
        captured: list[dict] = []
        body_chunks: list[bytes] = []
        status: int | None = None
        response_headers: list[tuple[bytes, bytes]] = []
        streaming = False
        flushed = False

        async def cap_send(event):
            nonlocal status, response_headers, streaming, flushed
            # The ``@as_middleware`` class decorator normalises ``call_next`` so
            # Response/JSONResponse objects from the handler arrive here as
            # plain start+body dict events.
            t = event.get('type')
            if t == ASGIEvent.HTTP_RESPONSE_START:
                status = event.get('status')
                response_headers = list(event.get('headers', []))
                captured.append(event)
                return  # don't send yet — wait for body so we can add ETag

            if t == ASGIEvent.HTTP_RESPONSE_BODY:
                body_chunks.append(event.get('body', b''))
                if event.get('more_body', False):
                    # Streaming starts here.  Flush whatever we have
                    # (start + this chunk) and switch to pass-through.
                    streaming = True
                    captured.append(event)
                    if not flushed:
                        for buf in captured:
                            await send(buf)
                        flushed = True
                    return
                # Final body chunk arrived; decide cacheability + ETag now.
                captured.append(event)
                if self._should_cache(status, response_headers):
                    body = b''.join(body_chunks)
                    etag = _read_etag(response_headers) or (
                        self._make_etag(body) if self._generate_etag else None)
                    if etag is not None and _read_etag(response_headers) is None:
                        # Inject the generated ETag into the start event
                        # *before* we send it; both the live response and
                        # the cached copy carry the same header.
                        start = captured[0]
                        new_hdrs = list(start.get('headers', []))
                        new_hdrs.append((b'etag', etag))
                        start['headers'] = new_hdrs
                    if etag is not None:
                        ttl = _response_max_age(response_headers) or self._max_age
                        self._store[key] = _Entry(
                            events=[dict(e) for e in captured],
                            etag=etag,
                            expires_at=time.monotonic() + ttl,
                        )
                        # LRU bound.
                        while len(self._store) > self._max_entries:
                            self._store.popitem(last=False)
                # Flush to client.
                for buf in captured:
                    await send(buf)
                flushed = True
                return

            # Anything else (trailers, etc) — pass through after the flush.
            captured.append(event)
            if flushed:
                await send(event)

        await call_next(scope, receive, cap_send)

        # If the handler never emitted a body event the response was
        # never flushed — forward whatever we have so the client at
        # least sees something.  Pathological case; not cached.
        if not flushed:
            for buf in captured:
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

def _request_headers(scope) -> dict[bytes, bytes]:
    """Index the request headers by lowercase name → value (first occurrence)."""
    out: dict[bytes, bytes] = {}
    for name, value in scope.get('headers', []):
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
                    pass
            elif t.startswith(b'max-age='):
                try:
                    max_age = int(t[8:])
                except ValueError:
                    pass
    return s_max if s_max is not None else max_age


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
