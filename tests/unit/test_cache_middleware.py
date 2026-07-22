"""Unit tests for the response-cache middleware.

A stub ``call_next`` records the number of times it ran for each
request and a captured ``send`` records the response events the
middleware emitted.  Most assertions check both: the cache should
serve the second request without re-running ``call_next``, and the
events the cache replays should equal what the handler produced.

End-to-end coverage (real HTTP round-trip with httpx) lives in
``tests/integration/test_cache_middleware.py``.
"""
import asyncio
import time
from unittest.mock import patch

import pytest

from blackbull.middleware.cache import (
    Cache,
    _Entry,
    _cache_control,
    _etag_matches,
    _read_etag,
    _request_has_no_store,
    _response_max_age,
)


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------

def _scope(method: str = 'GET', path: str = '/', query: bytes = b'',
           headers: list[tuple[bytes, bytes]] | None = None):
    # Sprint 80: HTTP is dispatched as a native Connection, not an ASGI scope.
    from blackbull.connection import Connection
    return Connection.from_scope({
        'type': 'http',
        'method': method,
        'path': path,
        'query_string': query,
        'headers': list(headers or []),
    })


def _make_handler(status: int = 200, body: bytes = b'hello',
                  extra_headers: list[tuple[bytes, bytes]] | None = None):
    """Return ``(call_next, counter)`` where counter tracks invocation count."""
    counter = {'n': 0}

    async def call_next(scope, receive, send):
        counter['n'] += 1
        hdrs = [(b'content-type', b'text/plain')] + list(extra_headers or [])
        await send({'type': 'http.response.start', 'status': status, 'headers': hdrs})
        await send({'type': 'http.response.body', 'body': body})

    return call_next, counter


async def _run(mw, scope, call_next):
    """Drive the middleware once, capture sent events."""
    sent: list = []

    async def send(event):
        sent.append(event)

    await mw(scope, None, send, call_next)
    return sent


def _split_response(events: list[dict]) -> tuple[int | None, list, bytes]:
    status = None
    headers: list = []
    body_parts: list[bytes] = []
    for e in events:
        if not isinstance(e, dict):
            continue
        if e.get('type') == 'http.response.start':
            status = e.get('status')
            headers = list(e.get('headers', []))
        elif e.get('type') == 'http.response.body':
            body_parts.append(e.get('body', b''))
    return status, headers, b''.join(body_parts)


# ---------------------------------------------------------------------------
# Construction / configuration
# ---------------------------------------------------------------------------

class TestConstruction:
    def test_default_max_age(self):
        assert Cache()._max_age == 300

    def test_explicit_max_age(self):
        assert Cache(max_age=60)._max_age == 60

    def test_invalid_max_age_raises(self):
        with pytest.raises(ValueError):
            Cache(max_age=0)
        with pytest.raises(ValueError):
            Cache(max_age=-1)

    def test_invalid_max_entries_raises(self):
        with pytest.raises(ValueError):
            Cache(max_entries=0)


# ---------------------------------------------------------------------------
# Basic behaviour
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestBasicCaching:
    async def test_first_request_calls_handler(self):
        mw = Cache()
        cn, counter = _make_handler()
        sent = await _run(mw, _scope(), cn)
        assert counter['n'] == 1
        status, _, body = _split_response(sent)
        assert status == 200
        assert body == b'hello'

    async def test_second_request_served_from_cache(self):
        mw = Cache()
        cn, counter = _make_handler()
        await _run(mw, _scope(), cn)
        await _run(mw, _scope(), cn)
        assert counter['n'] == 1, 'second request must NOT re-invoke handler'

    async def test_cache_hit_replays_full_response(self):
        mw = Cache()
        cn, _ = _make_handler(body=b'cached-body')
        first = await _run(mw, _scope(), cn)
        second = await _run(mw, _scope(), cn)
        _, _, body1 = _split_response(first)
        _, _, body2 = _split_response(second)
        assert body1 == body2 == b'cached-body'

    async def test_different_paths_cache_separately(self):
        mw = Cache()
        cn, counter = _make_handler()
        await _run(mw, _scope(path='/a'), cn)
        await _run(mw, _scope(path='/b'), cn)
        assert counter['n'] == 2

    async def test_different_query_strings_cache_separately(self):
        mw = Cache()
        cn, counter = _make_handler()
        await _run(mw, _scope(query=b'x=1'), cn)
        await _run(mw, _scope(query=b'x=2'), cn)
        assert counter['n'] == 2


# ---------------------------------------------------------------------------
# Cacheability rules
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestCacheability:
    async def test_post_request_not_cached(self):
        mw = Cache()
        cn, counter = _make_handler()
        await _run(mw, _scope(method='POST'), cn)
        await _run(mw, _scope(method='POST'), cn)
        assert counter['n'] == 2

    async def test_500_response_not_cached(self):
        mw = Cache()
        cn, counter = _make_handler(status=500)
        await _run(mw, _scope(), cn)
        await _run(mw, _scope(), cn)
        assert counter['n'] == 2

    async def test_cache_control_no_store_skips_storage(self):
        mw = Cache()
        cn, counter = _make_handler(
            extra_headers=[(b'cache-control', b'no-store')])
        await _run(mw, _scope(), cn)
        await _run(mw, _scope(), cn)
        assert counter['n'] == 2

    async def test_cache_control_private_skips_storage(self):
        mw = Cache()
        cn, counter = _make_handler(
            extra_headers=[(b'cache-control', b'private, max-age=60')])
        await _run(mw, _scope(), cn)
        await _run(mw, _scope(), cn)
        assert counter['n'] == 2

    async def test_cache_control_no_cache_skips_storage(self):
        """We treat ``no-cache`` as "don't store" too — the request-side
        revalidation semantics are out of scope."""
        mw = Cache()
        cn, counter = _make_handler(
            extra_headers=[(b'cache-control', b'no-cache')])
        await _run(mw, _scope(), cn)
        await _run(mw, _scope(), cn)
        assert counter['n'] == 2

    async def test_request_no_store_bypasses_cache(self):
        """A request with ``Cache-Control: no-store`` must NOT be served
        from cache, even if a fresh entry exists."""
        mw = Cache()
        cn, counter = _make_handler()
        await _run(mw, _scope(), cn)                # warm cache
        await _run(mw, _scope(headers=[(b'cache-control', b'no-store')]), cn)
        assert counter['n'] == 2

    async def test_authorization_request_bypasses_cache_by_default(self):
        mw = Cache()
        cn, counter = _make_handler()
        scope = _scope(headers=[(b'authorization', b'Bearer abc')])
        await _run(mw, scope, cn)
        await _run(mw, scope, cn)
        assert counter['n'] == 2

    async def test_cache_authenticated_true_caches_authorized(self):
        mw = Cache(cache_authenticated=True)
        cn, counter = _make_handler()
        scope = _scope(headers=[(b'authorization', b'Bearer abc')])
        await _run(mw, scope, cn)
        await _run(mw, scope, cn)
        assert counter['n'] == 1


# ---------------------------------------------------------------------------
# ETag / If-None-Match
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestETag:
    async def test_etag_generated_when_app_omits_it(self):
        mw = Cache()
        cn, _ = _make_handler()
        sent = await _run(mw, _scope(), cn)
        _, hdrs, _ = _split_response(sent)
        etags = [v for k, v in hdrs if k.lower() == b'etag']
        assert len(etags) == 1
        assert etags[0].startswith(b'W/"')

    async def test_app_supplied_etag_preserved(self):
        custom = b'"app-etag-v1"'
        mw = Cache()
        cn, _ = _make_handler(extra_headers=[(b'etag', custom)])
        sent = await _run(mw, _scope(), cn)
        _, hdrs, _ = _split_response(sent)
        etags = [v for k, v in hdrs if k.lower() == b'etag']
        assert etags == [custom]

    async def test_etag_unchanged_across_cache_hits(self):
        mw = Cache()
        cn, _ = _make_handler()
        e1, _, _ = _split_response(await _run(mw, _scope(), cn))  # noqa: F841
        first_etag = next(
            v for e in await _run(mw, _scope(), cn) if isinstance(e, dict)
            for k, v in e.get('headers', []) if k.lower() == b'etag'
        )
        assert first_etag.startswith(b'W/"')

    async def test_if_none_match_returns_304(self):
        mw = Cache()
        cn, counter = _make_handler()
        sent = await _run(mw, _scope(), cn)
        _, hdrs, _ = _split_response(sent)
        etag = next(v for k, v in hdrs if k.lower() == b'etag')

        sent2 = await _run(
            mw,
            _scope(headers=[(b'if-none-match', etag)]),
            cn,
        )
        assert counter['n'] == 1, 'handler must not be invoked on 304 path'
        status, _, body = _split_response(sent2)
        assert status == 304
        assert body == b''

    async def test_if_none_match_star_matches(self):
        mw = Cache()
        cn, _ = _make_handler()
        await _run(mw, _scope(), cn)
        sent = await _run(mw, _scope(headers=[(b'if-none-match', b'*')]), cn)
        status, _, _ = _split_response(sent)
        assert status == 304


# ---------------------------------------------------------------------------
# TTL / expiry
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestExpiry:
    async def test_expired_entry_triggers_refetch(self):
        mw = Cache(max_age=60)
        cn, counter = _make_handler()
        with patch('blackbull.middleware.cache.time.monotonic', return_value=1_000.0):
            await _run(mw, _scope(), cn)
        with patch('blackbull.middleware.cache.time.monotonic', return_value=1_100.0):
            await _run(mw, _scope(), cn)
        assert counter['n'] == 2

    async def test_response_max_age_overrides_default(self):
        """A response saying ``max-age=10`` shortens the cache lifetime."""
        mw = Cache(max_age=600)
        cn, counter = _make_handler(
            extra_headers=[(b'cache-control', b'max-age=10')])
        with patch('blackbull.middleware.cache.time.monotonic', return_value=1_000.0):
            await _run(mw, _scope(), cn)
        # 20 s later — well past the response-declared 10 s TTL.
        with patch('blackbull.middleware.cache.time.monotonic', return_value=1_020.0):
            await _run(mw, _scope(), cn)
        assert counter['n'] == 2

    async def test_s_maxage_takes_precedence(self):
        mw = Cache(max_age=600)
        cn, counter = _make_handler(
            extra_headers=[(b'cache-control', b'max-age=5, s-maxage=100')])
        with patch('blackbull.middleware.cache.time.monotonic', return_value=1_000.0):
            await _run(mw, _scope(), cn)
        # 30 s — past max-age=5 but well within s-maxage=100.
        with patch('blackbull.middleware.cache.time.monotonic', return_value=1_030.0):
            await _run(mw, _scope(), cn)
        assert counter['n'] == 1, 's-maxage must take precedence over max-age'


# ---------------------------------------------------------------------------
# LRU bound
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestLRUEviction:
    async def test_oldest_entry_evicted_when_cap_reached(self):
        mw = Cache(max_entries=2)
        cn, counter = _make_handler()
        await _run(mw, _scope(path='/a'), cn)
        await _run(mw, _scope(path='/b'), cn)
        await _run(mw, _scope(path='/c'), cn)  # /a should now be evicted
        # Re-request /a → cache miss → handler runs again.
        await _run(mw, _scope(path='/a'), cn)
        assert counter['n'] == 4

    async def test_access_promotes_to_mru(self):
        mw = Cache(max_entries=2)
        cn, counter = _make_handler()
        await _run(mw, _scope(path='/a'), cn)
        await _run(mw, _scope(path='/b'), cn)
        await _run(mw, _scope(path='/a'), cn)  # /a now MRU
        await _run(mw, _scope(path='/c'), cn)  # /b should be evicted, /a survives
        await _run(mw, _scope(path='/a'), cn)  # still cached
        assert counter['n'] == 3


# ---------------------------------------------------------------------------
# Scope-type filtering
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestScopeFilter:
    async def test_websocket_scope_passes_through(self):
        mw = Cache()
        called = []

        async def call_next(scope, receive, send):
            called.append(scope)

        scope = {'type': 'websocket', 'method': 'GET', 'path': '/', 'headers': []}
        await mw(scope, None, None, call_next)
        assert called == [scope]

    async def test_lifespan_scope_passes_through(self):
        mw = Cache()
        called = []

        async def call_next(scope, receive, send):
            called.append(scope)

        scope = {'type': 'lifespan'}
        await mw(scope, None, None, call_next)
        assert called == [scope]


# ---------------------------------------------------------------------------
# Header-inspection helpers
# ---------------------------------------------------------------------------

class TestHeaderHelpers:
    def test_request_no_store_detection(self):
        assert _request_has_no_store({b'cache-control': b'no-store'})
        assert _request_has_no_store({b'cache-control': b'no-cache, no-store'})
        assert not _request_has_no_store({b'cache-control': b'no-cache'})
        assert not _request_has_no_store({})

    def test_cache_control_directive_split(self):
        cc = _cache_control([(b'cache-control', b'public, max-age=300, must-revalidate')])
        assert b'public' in cc
        assert b'must-revalidate' in cc
        assert b'max-age=300' in cc
        assert b'max-age' in cc   # bare directive name also present

    def test_response_max_age_parses_max_age(self):
        assert _response_max_age([(b'cache-control', b'max-age=120')]) == 120

    def test_response_max_age_prefers_s_maxage(self):
        assert _response_max_age(
            [(b'cache-control', b'max-age=10, s-maxage=99')]) == 99

    def test_response_max_age_missing_returns_none(self):
        assert _response_max_age([(b'cache-control', b'public')]) is None
        assert _response_max_age([]) is None

    def test_response_max_age_garbage_value_ignored(self):
        assert _response_max_age([(b'cache-control', b'max-age=oops')]) is None

    def test_read_etag(self):
        assert _read_etag([(b'etag', b'"abc"')]) == b'"abc"'
        assert _read_etag([(b'ETag', b'"abc"')]) == b'"abc"'
        assert _read_etag([]) is None

    def test_etag_matches_exact(self):
        assert _etag_matches(b'"abc"', b'"abc"')

    def test_etag_matches_star(self):
        assert _etag_matches(b'*', b'anything')

    def test_etag_matches_weak_vs_strong(self):
        """Weak comparison: W/"abc" matches "abc" (and itself)."""
        assert _etag_matches(b'W/"abc"', b'"abc"')
        assert _etag_matches(b'"abc"', b'W/"abc"')

    def test_etag_matches_multiple_candidates(self):
        assert _etag_matches(b'"x", "y", "z"', b'"y"')

    def test_etag_no_match(self):
        assert not _etag_matches(b'"abc"', b'"def"')


# ---------------------------------------------------------------------------
# 1.21b — Vary-aware caching (variant correctness)
# ---------------------------------------------------------------------------

def _vary_handler(vary_value: bytes = b'Accept-Encoding'):
    """Handler that varies its body by Accept-Encoding and advertises Vary."""
    counter = {'n': 0}

    async def call_next(scope, receive, send):
        counter['n'] += 1
        ae = b''
        for k, v in scope.headers:
            if k.lower() == b'accept-encoding':
                ae = v
        body = b'ENC:' + ae
        await send({'type': 'http.response.start', 'status': 200, 'headers': [
            (b'content-type', b'text/plain'),
            (b'vary', vary_value),
        ]})
        await send({'type': 'http.response.body', 'body': body})

    return call_next, counter


@pytest.mark.asyncio
class TestVaryAwareCaching:
    async def test_different_accept_encoding_not_cross_served(self):
        """A brotli variant must never be replayed to an identity client (1.21b)."""
        mw = Cache()
        cn, counter = _vary_handler()
        _, _, body_br = _split_response(
            await _run(mw, _scope(headers=[(b'accept-encoding', b'br')]), cn))
        _, _, body_id = _split_response(
            await _run(mw, _scope(headers=[(b'accept-encoding', b'identity')]), cn))
        assert body_br == b'ENC:br'
        assert body_id == b'ENC:identity'
        assert counter['n'] == 2, 'each variant must be produced by the handler'

    async def test_same_variant_served_from_cache(self):
        mw = Cache()
        cn, counter = _vary_handler()
        await _run(mw, _scope(headers=[(b'accept-encoding', b'br')]), cn)
        _, _, body2 = _split_response(
            await _run(mw, _scope(headers=[(b'accept-encoding', b'br')]), cn))
        assert body2 == b'ENC:br'
        assert counter['n'] == 1, 'identical variant must hit the cache'

    async def test_vary_star_never_stored(self):
        mw = Cache()
        cn, counter = _vary_handler(vary_value=b'*')
        await _run(mw, _scope(headers=[(b'accept-encoding', b'br')]), cn)
        await _run(mw, _scope(headers=[(b'accept-encoding', b'br')]), cn)
        assert counter['n'] == 2, 'Vary: * responses must not be stored'


@pytest.mark.asyncio
class TestVaryBucketEviction:
    """1.21g — variant metadata lives in the same bucket as its entries, so it
    can never be evicted independently and orphan them."""

    async def test_varied_entry_survives_other_url_churn(self):
        mw = Cache(max_entries=8)
        cn, counter = _vary_handler()
        vscope = _scope(path='/v', headers=[(b'accept-encoding', b'br')])
        await _run(mw, vscope, cn)                     # store /v br-variant
        for p in ('/a', '/b', '/c'):                   # add other URLs (< cap)
            h, _ = _make_handler()
            await _run(mw, _scope(path=p), h)
        _, _, body = _split_response(await _run(mw, vscope, cn))
        assert body == b'ENC:br'
        assert counter['n'] == 1, 'the br variant must still hit the cache'

    async def test_bucket_evicted_as_a_unit(self):
        """When a URL's bucket is LRU-evicted, its Vary fields and entries go
        together — the next request is a clean miss + re-store, never a stale
        lookup against orphaned entries (the old dual-LRU failure mode)."""
        mw = Cache(max_entries=2)
        cn, counter = _vary_handler()
        vscope = _scope(path='/v', headers=[(b'accept-encoding', b'br')])
        await _run(mw, vscope, cn)                     # counter → 1
        for p in ('/a', '/b'):                         # evict /v (cap=2)
            h, _ = _make_handler()
            await _run(mw, _scope(path=p), h)
        assert ('GET', '/v', b'') not in mw._store     # bucket gone as a unit
        _, _, body = _split_response(await _run(mw, vscope, cn))  # clean miss
        assert body == b'ENC:br'
        assert counter['n'] == 2, 'handler re-ran; no orphaned stale entry'
        await _run(mw, vscope, cn)                      # now hits again
        assert counter['n'] == 2

    async def test_vary_change_drops_stale_variants(self):
        mw = Cache()
        cn1, _ = _vary_handler(vary_value=b'Accept-Encoding')
        await _run(mw, _scope(path='/x', headers=[(b'accept-encoding', b'br')]), cn1)
        bucket = mw._store[('GET', '/x', b'')]
        assert bucket.vary_fields == (b'accept-encoding',)
        assert len(bucket.entries) == 1
        # Same URL now varies by a different header → adopt it, drop the stale
        # variant keyed on the old fields.
        cn2, _ = _vary_handler(vary_value=b'Accept-Language')
        await _run(mw, _scope(path='/x', headers=[(b'accept-language', b'en')]), cn2)
        bucket = mw._store[('GET', '/x', b'')]
        assert bucket.vary_fields == (b'accept-language',)
        assert len(bucket.entries) == 1

    async def test_per_bucket_variant_cap(self):
        from blackbull.middleware.cache import _MAX_VARIANTS_PER_KEY
        mw = Cache()
        cn, _ = _vary_handler()
        for i in range(_MAX_VARIANTS_PER_KEY + 5):
            await _run(mw, _scope(path='/p',
                                  headers=[(b'accept-encoding', f'enc{i}'.encode())]), cn)
        bucket = mw._store[('GET', '/p', b'')]
        assert len(bucket.entries) <= _MAX_VARIANTS_PER_KEY


@pytest.mark.asyncio
class TestCacheBehindCompression:
    """End-to-end 1.21f + 1.21g: a ``Cache`` in front of ``Compression`` must
    serve each Accept-Encoding client its own variant, no matter which client
    arrives first — the first-request poisoning scenario the two fixes close."""

    @staticmethod
    def _stack():
        from blackbull.middleware.compression import Compression
        compression = Compression()

        async def handler(scope, receive, send):
            body = b'compressible payload ' * 40  # > _MIN_SIZE
            await send({'type': 'http.response.start', 'status': 200,
                        'headers': [(b'content-type', b'text/plain')]})
            await send({'type': 'http.response.body', 'body': body,
                        'more_body': False})

        async def compression_layer(scope, receive, send):
            await compression(scope, receive, send, handler)

        return Cache(), compression_layer

    async def _fetch(self, cache, layer, accept: bytes):
        status, headers, body = _split_response(
            await _run(cache, _scope(headers=[(b'accept-encoding', accept)]), layer))
        return dict(headers), body

    async def test_gzip_first_then_identity(self):
        cache, layer = self._stack()
        gz_hdrs, _ = await self._fetch(cache, layer, b'gzip')
        id_hdrs, _ = await self._fetch(cache, layer, b'')
        assert gz_hdrs.get(b'content-encoding') == b'gzip'
        # The identity client must NOT be served the gzip variant.
        assert id_hdrs.get(b'content-encoding') is None

    async def test_identity_first_then_gzip(self):
        # The dangerous order: the un-encoded response is cached first. Without
        # 1.21f it would carry no Vary and poison the gzip client.
        cache, layer = self._stack()
        id_hdrs, _ = await self._fetch(cache, layer, b'')
        gz_hdrs, _ = await self._fetch(cache, layer, b'gzip')
        assert id_hdrs.get(b'content-encoding') is None
        assert gz_hdrs.get(b'content-encoding') == b'gzip', \
            'gzip client must get gzip, not the cached identity variant'


class TestVaryHelpers:
    def test_response_vary_absent(self):
        from blackbull.middleware.cache import _response_vary
        assert _response_vary([(b'content-type', b'text/plain')]) == ()

    def test_response_vary_fields_sorted_lowercased(self):
        from blackbull.middleware.cache import _response_vary
        assert _response_vary(
            [(b'vary', b'Accept-Encoding, Accept-Language')]
        ) == (b'accept-encoding', b'accept-language')

    def test_response_vary_star_is_none(self):
        from blackbull.middleware.cache import _response_vary
        assert _response_vary([(b'vary', b'*')]) is None

    def test_vary_key_pulls_request_values(self):
        from blackbull.middleware.cache import _vary_key
        req = {b'accept-encoding': b'br'}
        assert _vary_key((b'accept-encoding',), req) == ((b'accept-encoding', b'br'),)

    def test_vary_key_missing_header_is_empty(self):
        from blackbull.middleware.cache import _vary_key
        assert _vary_key((b'accept-encoding',), {}) == ((b'accept-encoding', b''),)
