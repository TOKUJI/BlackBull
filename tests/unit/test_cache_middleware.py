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

from blackbull.native import NativeResponse
from blackbull.middleware.cache import (
    Cache,
    _Capture,
    _directives,
    _etag_matches,
    _must_not_store,
    _names,
    _parse_directives,
    _readable,
    _response_etag,
    _smallest,
    _stated_freshness,
    _stated_max_age,
)


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------

def _scope(method: str = 'GET', path: str = '/', query: bytes = b'',
           headers: list[tuple[bytes, bytes]] | None = None):
    # HTTP is dispatched as a native Connection, not an ASGI scope.
    from blackbull.connection import Connection
    return Connection.from_scope({
        'type': 'http',
        'method': method,
        'path': path,
        'query_string': query,
        'headers': list(headers or []),
        'server': ('testserver', 80),
    })


def _ws_scope(path: str = '/'):
    """A WebSocket handshake, as the H/1 actor marks one before dispatch."""
    from blackbull.connection import Connection
    return Connection.from_scope({
        'type': 'websocket',
        'method': 'GET',
        'path': path,
        'query_string': b'',
        'headers': [(b'upgrade', b'websocket'),
                    (b'sec-websocket-key', b'x' * 24),
                    (b'sec-websocket-version', b'13')],
        'server': ('testserver', 80),
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
        # Cache stores and replays native objects; these tests assert on the
        # ASGI event shape, so the seam is normalised away here (same
        # convention as ``test_middlewares._collect``).
        from blackbull.native import NativeResponse
        if isinstance(event, NativeResponse):
            sent.extend(event.to_asgi())
        else:
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
# Stated freshness and the request's own directives (RFC 9111 §4.2, §5.2)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestExplicitFreshness:
    """The configured default must not widen a freshness the response stated."""

    async def test_zero_max_age_is_stale_at_once(self):
        mw = Cache(max_age=600)
        cn, counter = _make_handler(
            extra_headers=[(b'cache-control', b'max-age=0')])
        with patch('blackbull.middleware.cache.time.monotonic', return_value=1_000.0):
            await _run(mw, _scope(), cn)
        with patch('blackbull.middleware.cache.time.monotonic', return_value=1_000.5):
            await _run(mw, _scope(), cn)
        assert counter['n'] == 2

    async def test_zero_s_maxage_wins_over_positive_max_age(self):
        mw = Cache(max_age=600)
        cn, counter = _make_handler(
            extra_headers=[(b'cache-control', b'max-age=120, s-maxage=0')])
        with patch('blackbull.middleware.cache.time.monotonic', return_value=1_000.0):
            await _run(mw, _scope(), cn)
        with patch('blackbull.middleware.cache.time.monotonic', return_value=1_000.5):
            await _run(mw, _scope(), cn)
        assert counter['n'] == 2

    async def test_absent_freshness_keeps_the_configured_default(self):
        mw = Cache(max_age=600)
        cn, counter = _make_handler()
        with patch('blackbull.middleware.cache.time.monotonic', return_value=1_000.0):
            await _run(mw, _scope(), cn)
        with patch('blackbull.middleware.cache.time.monotonic', return_value=1_500.0):
            await _run(mw, _scope(), cn)
        assert counter['n'] == 1

    async def test_malformed_freshness_keeps_the_configured_default(self):
        mw = Cache(max_age=600)
        cn, counter = _make_handler(
            extra_headers=[(b'cache-control', b'max-age=abc')])
        with patch('blackbull.middleware.cache.time.monotonic', return_value=1_000.0):
            await _run(mw, _scope(), cn)
        with patch('blackbull.middleware.cache.time.monotonic', return_value=1_500.0):
            await _run(mw, _scope(), cn)
        assert counter['n'] == 1

    async def test_negative_freshness_is_stale_not_defaulted(self):
        mw = Cache(max_age=600)
        cn, counter = _make_handler(
            extra_headers=[(b'cache-control', b'max-age=-5')])
        with patch('blackbull.middleware.cache.time.monotonic', return_value=1_000.0):
            await _run(mw, _scope(), cn)
        with patch('blackbull.middleware.cache.time.monotonic', return_value=1_000.5):
            await _run(mw, _scope(), cn)
        assert counter['n'] == 2

    async def test_every_cache_control_field_is_read(self):
        mw = Cache(max_age=600)
        cn, counter = _make_handler(extra_headers=[
            (b'cache-control', b'public'), (b'cache-control', b'max-age=0')])
        with patch('blackbull.middleware.cache.time.monotonic', return_value=1_000.0):
            await _run(mw, _scope(), cn)
        with patch('blackbull.middleware.cache.time.monotonic', return_value=1_000.5):
            await _run(mw, _scope(), cn)
        assert counter['n'] == 2


@pytest.mark.asyncio
class TestStatedFreshnessSources:
    """Every place a response can state freshness, and one that states age."""

    async def test_expires_only_response_is_as_stale_as_it_says(self):
        mw = Cache(max_age=600)
        cn, counter = _make_handler(extra_headers=[
            (b'expires', b'Thu, 01 Jan 1970 00:00:00 GMT')])
        await _run(mw, _scope(), cn)
        await _run(mw, _scope(), cn)
        assert counter['n'] == 2

    async def test_expires_lifetime_is_measured_against_date(self):
        mw = Cache(max_age=600)
        cn, counter = _make_handler(extra_headers=[
            (b'date', b'Mon, 01 Jan 2024 00:00:00 GMT'),
            (b'expires', b'Mon, 01 Jan 2024 00:00:10 GMT')])
        with patch('blackbull.middleware.cache.time.monotonic', return_value=1_000.0):
            await _run(mw, _scope(), cn)
        with patch('blackbull.middleware.cache.time.monotonic', return_value=1_005.0):
            await _run(mw, _scope(), cn)
        assert counter['n'] == 1
        with patch('blackbull.middleware.cache.time.monotonic', return_value=1_020.0):
            await _run(mw, _scope(), cn)
        assert counter['n'] == 2

    async def test_max_age_wins_over_expires(self):
        mw = Cache(max_age=600)
        cn, counter = _make_handler(extra_headers=[
            (b'cache-control', b'max-age=60'),
            (b'expires', b'Thu, 01 Jan 1970 00:00:00 GMT')])
        with patch('blackbull.middleware.cache.time.monotonic', return_value=1_000.0):
            await _run(mw, _scope(), cn)
        with patch('blackbull.middleware.cache.time.monotonic', return_value=1_030.0):
            await _run(mw, _scope(), cn)
        assert counter['n'] == 1

    async def test_an_incoming_age_counts_against_the_lifetime(self):
        mw = Cache()
        cn, counter = _make_handler(extra_headers=[
            (b'cache-control', b'max-age=600'), (b'age', b'10000')])
        await _run(mw, _scope(), cn)
        await _run(mw, _scope(), cn)
        assert counter['n'] == 2

    async def test_an_absurd_stated_lifetime_does_not_break_the_response(self):
        mw = Cache(max_age=600)
        cn, counter = _make_handler(extra_headers=[
            (b'cache-control', b'max-age=' + b'9' * 400)])
        sent = await _run(mw, _scope(), cn)
        assert _split_response(sent)[0] == 200
        await _run(mw, _scope(), cn)          # clamped short, still fresh
        assert counter['n'] == 1

    async def test_quoted_delta_seconds_are_read(self):
        mw = Cache(max_age=600)
        cn, counter = _make_handler(
            extra_headers=[(b'cache-control', b'max-age="0"')])
        with patch('blackbull.middleware.cache.time.monotonic', return_value=1_000.0):
            await _run(mw, _scope(), cn)
        with patch('blackbull.middleware.cache.time.monotonic', return_value=1_000.5):
            await _run(mw, _scope(), cn)
        assert counter['n'] == 2

    async def test_a_quoted_value_cannot_inject_a_directive(self):
        """``x="a,max-age=31536000,b"`` is one extension directive whose value
        mentions max-age — not a stated lifetime."""
        mw = Cache(max_age=300)
        cn, counter = _make_handler(extra_headers=[
            (b'cache-control', b'x="a,max-age=31536000,b"')])
        with patch('blackbull.middleware.cache.time.monotonic', return_value=1_000.0):
            await _run(mw, _scope(), cn)
        with patch('blackbull.middleware.cache.time.monotonic', return_value=1_400.0):
            await _run(mw, _scope(), cn)
        assert counter['n'] == 2

    async def test_repeated_expires_takes_the_earliest(self):
        far = b'Fri, 31 Dec 9999 23:59:59 GMT'
        past = b'Thu, 01 Jan 1970 00:00:00 GMT'
        for headers in ([(b'expires', past), (b'expires', far)],
                        [(b'expires', far), (b'expires', past)]):
            mw = Cache(max_age=300)
            cn, counter = _make_handler(extra_headers=headers)
            with patch('blackbull.middleware.cache.time.monotonic',
                       return_value=1_000.0):
                await _run(mw, _scope(), cn)
            with patch('blackbull.middleware.cache.time.monotonic',
                       return_value=1_400.0):
                await _run(mw, _scope(), cn)
            assert counter['n'] == 2, headers

    async def test_replayed_response_carries_its_current_age(self):
        mw = Cache()
        cn, _ = _make_handler(extra_headers=[
            (b'cache-control', b'max-age=600'), (b'age', b'100')])
        with patch('blackbull.middleware.cache.time.monotonic', return_value=1_000.0):
            await _run(mw, _scope(), cn)
        with patch('blackbull.middleware.cache.time.monotonic', return_value=1_400.0):
            sent = await _run(mw, _scope(), cn)
        ages = [v for n, v in _split_response(sent)[1] if n.lower() == b'age']
        assert ages == [b'500'], ages

    async def test_a_regressed_clock_does_not_emit_a_negative_age(self):
        mw = Cache()
        cn, _ = _make_handler(extra_headers=[(b'cache-control', b'max-age=600')])
        with patch('blackbull.middleware.cache.time.monotonic', return_value=1_000.0):
            await _run(mw, _scope(), cn)
        with patch('blackbull.middleware.cache.time.monotonic', return_value=995.0):
            sent = await _run(mw, _scope(), cn)
        ages = [v for n, v in _split_response(sent)[1] if n.lower() == b'age']
        assert ages == [b'0'], ages

    async def test_a_field_we_cannot_parse_is_not_stored(self):
        """An unterminated quote makes the field unreadable, so the response is
        not stored rather than read through the broken text."""
        mw = Cache(max_age=300)
        cn, counter = _make_handler(extra_headers=[
            (b'cache-control', b'x="a,max-age=31536000')])
        with patch('blackbull.middleware.cache.time.monotonic', return_value=1_000.0):
            await _run(mw, _scope(), cn)
        with patch('blackbull.middleware.cache.time.monotonic', return_value=1_400.0):
            await _run(mw, _scope(), cn)
        assert counter['n'] == 2

    async def test_a_misplaced_quote_is_not_stored(self):
        """``"no-cache"`` is balanced, so a reader that only counts quotes
        would store the response — the cache must not."""
        mw = Cache(max_age=600)
        cn, counter = _make_handler(
            extra_headers=[(b'cache-control', b'"no-cache"')])
        with patch('blackbull.middleware.cache.time.monotonic', return_value=1_000.0):
            await _run(mw, _scope(), cn)
        with patch('blackbull.middleware.cache.time.monotonic', return_value=1_000.5):
            await _run(mw, _scope(), cn)
        assert counter['n'] == 2

    async def test_a_quoted_value_that_is_no_number_states_no_lifetime(self):
        """``max-age="31536000\\"x"`` de-escapes to a string that is not a
        number, so its leading digits are not the lifetime; the default stands
        and expires at 600 s, not after a year."""
        mw = Cache(max_age=600)
        cn, counter = _make_handler(
            extra_headers=[(b'cache-control', b'max-age="31536000\\"x"')])
        with patch('blackbull.middleware.cache.time.monotonic', return_value=1_000.0):
            await _run(mw, _scope(), cn)
        with patch('blackbull.middleware.cache.time.monotonic', return_value=1_700.0):
            await _run(mw, _scope(), cn)
        assert counter['n'] == 2

    async def test_repeated_max_age_takes_the_most_restrictive_value(self):
        mw = Cache(max_age=600)
        cn, counter = _make_handler()
        await _run(mw, _scope(), cn)
        await _run(mw, _scope(headers=[
            (b'cache-control', b'max-age=600, max-age=0')]), cn)
        assert counter['n'] == 2


@pytest.mark.asyncio
class TestRequestDirectives:
    """The request's own Cache-Control binds what a hit may reuse."""

    async def test_request_no_cache_runs_the_handler(self):
        mw = Cache()
        cn, counter = _make_handler()
        await _run(mw, _scope(), cn)
        await _run(mw, _scope(headers=[(b'cache-control', b'no-cache')]), cn)
        assert counter['n'] == 2

    async def test_request_no_cache_is_not_answered_304_by_the_cache(self):
        """A conditional request with ``no-cache`` is the origin's to answer:
        the stored copy must not be turned into a 304 behind its back."""
        mw = Cache()
        cn, counter = _make_handler()
        first = await _run(mw, _scope(), cn)
        etag = next(v for n, v in _split_response(first)[1]
                    if n.lower() == b'etag')

        sent = await _run(mw, _scope(headers=[
            (b'cache-control', b'no-cache'), (b'if-none-match', etag)]), cn)

        status, _, body = _split_response(sent)
        assert counter['n'] == 2
        assert (status, body) == (200, b'hello')

    async def test_request_max_age_bounds_the_age_it_accepts(self):
        mw = Cache()
        cn, counter = _make_handler()
        with patch('blackbull.middleware.cache.time.monotonic', return_value=1_000.0):
            await _run(mw, _scope(), cn)
        # 3 s old, the client accepts 60 s → the stored copy still answers.
        with patch('blackbull.middleware.cache.time.monotonic', return_value=1_003.0):
            await _run(mw, _scope(headers=[(b'cache-control', b'max-age=60')]), cn)
        assert counter['n'] == 1
        # 10 s old, the client accepts 5 s → the handler runs.
        with patch('blackbull.middleware.cache.time.monotonic', return_value=1_010.0):
            await _run(mw, _scope(headers=[(b'cache-control', b'max-age=5')]), cn)
        assert counter['n'] == 2

    async def test_a_second_cache_control_field_still_binds(self):
        mw = Cache()
        cn, counter = _make_handler()
        await _run(mw, _scope(), cn)
        await _run(mw, _scope(headers=[(b'cache-control', b'max-age=600'),
                                       (b'cache-control', b'no-cache')]), cn)
        assert counter['n'] == 2

    async def test_a_second_field_no_store_bypasses(self):
        mw = Cache()
        cn, counter = _make_handler()
        await _run(mw, _scope(), cn)
        await _run(mw, _scope(headers=[(b'cache-control', b'public'),
                                       (b'cache-control', b'no-store')]), cn)
        assert counter['n'] == 2

    async def test_pragma_no_cache_validates_when_cache_control_is_absent(self):
        mw = Cache()
        cn, counter = _make_handler()
        await _run(mw, _scope(), cn)
        await _run(mw, _scope(headers=[(b'pragma', b'no-cache')]), cn)
        assert counter['n'] == 2

    @pytest.mark.parametrize('cache_control', [b'max-age=600', b''])
    async def test_pragma_is_ignored_when_cache_control_is_present(self, cache_control):
        mw = Cache()
        cn, counter = _make_handler()
        await _run(mw, _scope(), cn)
        await _run(mw, _scope(headers=[(b'cache-control', cache_control),
                                       (b'pragma', b'no-cache')]), cn)
        assert counter['n'] == 1

    async def test_a_quoted_request_max_age_is_read(self):
        mw = Cache()
        cn, counter = _make_handler()
        with patch('blackbull.middleware.cache.time.monotonic', return_value=1_000.0):
            await _run(mw, _scope(), cn)
        with patch('blackbull.middleware.cache.time.monotonic', return_value=1_000.5):
            await _run(mw, _scope(headers=[(b'cache-control', b'max-age="0"')]), cn)
        assert counter['n'] == 2

    async def test_pragma_no_cache_as_a_list_or_second_field(self):
        mw = Cache()
        cn, counter = _make_handler()
        await _run(mw, _scope(), cn)
        await _run(mw, _scope(headers=[(b'pragma', b'no-cache, x')]), cn)
        assert counter['n'] == 2
        await _run(mw, _scope(headers=[(b'pragma', b'x'),
                                       (b'pragma', b'no-cache')]), cn)
        assert counter['n'] == 3

    async def test_qualified_request_no_cache_still_validates(self):
        mw = Cache()
        cn, counter = _make_handler()
        await _run(mw, _scope(), cn)
        await _run(mw, _scope(headers=[
            (b'cache-control', b'no-cache="set-cookie"')]), cn)
        assert counter['n'] == 2

    async def test_a_stray_quote_does_not_hide_a_later_directive(self):
        """The field cannot be read, so the response is not stored — and the
        ``max-age=0`` behind the stray quote is never read past."""
        mw = Cache(max_age=600)
        cn, counter = _make_handler(
            extra_headers=[(b'cache-control', b'x", max-age=0')])
        with patch('blackbull.middleware.cache.time.monotonic', return_value=1_000.0):
            await _run(mw, _scope(), cn)
        with patch('blackbull.middleware.cache.time.monotonic', return_value=1_000.5):
            await _run(mw, _scope(), cn)
        assert counter['n'] == 2

    async def test_a_trailing_stray_quote_leaves_the_field_unreadable(self):
        mw = Cache(max_age=600)
        cn, counter = _make_handler(
            extra_headers=[(b'cache-control', b'no-cache"')])
        await _run(mw, _scope(), cn)
        await _run(mw, _scope(), cn)
        assert counter['n'] == 2

        mw = Cache(max_age=600)
        cn, counter = _make_handler(
            extra_headers=[(b'cache-control', b'max-age=0"')])
        with patch('blackbull.middleware.cache.time.monotonic', return_value=1_000.0):
            await _run(mw, _scope(), cn)
        with patch('blackbull.middleware.cache.time.monotonic', return_value=1_000.5):
            await _run(mw, _scope(), cn)
        assert counter['n'] == 2

    async def test_no_store_is_matched_by_name_not_inside_a_value(self):
        """``no-store="a,b"`` asks for it; a mention inside another directive's
        quoted value does not."""
        mw = Cache()
        cn, counter = _make_handler()
        await _run(mw, _scope(), cn)
        await _run(mw, _scope(headers=[
            (b'cache-control', b'no-store="a,b"')]), cn)
        assert counter['n'] == 2
        await _run(mw, _scope(headers=[
            (b'cache-control', b'x="a,no-store,b"')]), cn)
        assert counter['n'] == 2, 'the mentioned no-store must not bypass'

    async def test_no_store_with_a_stray_quote_still_bypasses(self):
        mw = Cache()
        cn, counter = _make_handler()
        await _run(mw, _scope(), cn)
        await _run(mw, _scope(headers=[(b'cache-control', b'no-store"')]), cn)
        assert counter['n'] == 2

    async def test_a_quote_inside_a_name_is_not_stored(self):
        """A quote where the grammar has none makes the field unreadable, so a
        ``no-cache`` written that way is not read through."""
        mw = Cache(max_age=300)
        cn, counter = _make_handler(
            extra_headers=[(b'cache-control', b'no-cache"x"')])
        await _run(mw, _scope(), cn)
        await _run(mw, _scope(), cn)
        assert counter['n'] == 2

    async def test_a_quote_inside_a_request_name_validates(self):
        mw = Cache()
        cn, counter = _make_handler()
        await _run(mw, _scope(), cn)
        await _run(mw, _scope(headers=[(b'cache-control', b'no-cache"x"')]), cn)
        assert counter['n'] == 2

    async def test_a_value_that_does_not_end_at_a_comma_is_not_stored(self):
        """``max-age="0"x`` states nothing a reader can trust, so it is not
        read as "unstated" and given the default either."""
        mw = Cache(max_age=600)
        cn, counter = _make_handler(
            extra_headers=[(b'cache-control', b'max-age="0"x')])
        with patch('blackbull.middleware.cache.time.monotonic', return_value=1_000.0):
            await _run(mw, _scope(), cn)
        with patch('blackbull.middleware.cache.time.monotonic', return_value=1_000.5):
            await _run(mw, _scope(), cn)
        assert counter['n'] == 2

    async def test_a_request_value_that_does_not_end_at_a_comma_validates(self):
        mw = Cache()
        cn, counter = _make_handler()
        await _run(mw, _scope(), cn)
        await _run(mw, _scope(headers=[(b'cache-control', b'max-age="0"x')]), cn)
        assert counter['n'] == 2

    async def test_a_misplaced_quote_in_a_request_validates(self):
        mw = Cache()
        cn, counter = _make_handler()
        await _run(mw, _scope(), cn)
        await _run(mw, _scope(headers=[(b'cache-control', b'"no-cache"')]), cn)
        assert counter['n'] == 2

    async def test_an_unreadable_request_field_is_not_stored(self):
        """It could be stating a ``no-store`` this cache failed to read, so the
        response it produced must not answer the next request."""
        mw = Cache(max_age=600)
        cn, counter = _make_handler()
        await _run(mw, _scope(headers=[(b'cache-control', b'"no-store"')]), cn)
        await _run(mw, _scope(), cn)
        assert counter['n'] == 2

    async def test_a_quoted_pair_keeps_the_field_usable(self):
        """``x="a\"b"`` is legal, so the response is stored with its stated
        lifetime instead of being refused."""
        mw = Cache()
        cn, counter = _make_handler(extra_headers=[
            (b'cache-control', b'x="a\\"b", max-age=600')])
        with patch('blackbull.middleware.cache.time.monotonic', return_value=1_000.0):
            await _run(mw, _scope(), cn)
        with patch('blackbull.middleware.cache.time.monotonic', return_value=1_400.0):
            await _run(mw, _scope(), cn)
        assert counter['n'] == 1

    async def test_an_escaped_closing_quote_leaves_the_field_unreadable(self):
        """The quote that would close the value is escaped, so the string is
        still open: not a one-year lifetime."""
        mw = Cache(max_age=300)
        cn, counter = _make_handler(extra_headers=[
            (b'cache-control', b'max-age="31536000\\"')])
        with patch('blackbull.middleware.cache.time.monotonic', return_value=1_000.0):
            await _run(mw, _scope(), cn)
        with patch('blackbull.middleware.cache.time.monotonic', return_value=1_400.0):
            await _run(mw, _scope(), cn)
        assert counter['n'] == 2

    async def test_request_max_age_zero_always_validates(self):
        mw = Cache()
        cn, counter = _make_handler()
        with patch('blackbull.middleware.cache.time.monotonic', return_value=1_000.0):
            await _run(mw, _scope(), cn)
        with patch('blackbull.middleware.cache.time.monotonic', return_value=1_000.5):
            await _run(mw, _scope(headers=[(b'cache-control', b'max-age=0')]), cn)
        assert counter['n'] == 2


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
class TestNonHTTPRequests:
    """Cache serves HTTP requests; everything else goes to the handler."""

    async def test_a_websocket_handshake_is_not_answered_from_the_cache(self):
        """The app hands middleware a native Connection of the request's own
        type, so a handshake must not be served the cached GET for its URL."""
        mw = Cache(max_age=600)
        cn, counter = _make_handler()
        await _run(mw, _scope(path='/chat'), cn)
        await _run(mw, _ws_scope(path='/chat'), cn)
        assert counter['n'] == 2

    async def test_a_response_with_trailers_is_passed_through_unstored(self):
        mw = Cache(max_age=600)
        calls = {'n': 0}

        async def call_next(scope, receive, send):
            calls['n'] += 1
            await send(NativeResponse(
                status=200, header=[(b'content-type', b'text/plain')],
                body=b'hello', expects_trailers=True))
            await send(NativeResponse(trailers=[(b'x-sum', b'1')]))

        sent: list = []

        async def send(event):
            sent.append(event)

        await mw(_scope(), None, send, call_next)
        assert [(e.trailers) for e in sent] == [None, [(b'x-sum', b'1')]]
        await mw(_scope(), None, send, call_next)
        assert calls['n'] == 2


@pytest.mark.asyncio
class TestOriginKeying:
    """The key is per origin: two hosts never share one copy."""

    async def test_hosts_do_not_share_an_entry(self):
        mw = Cache(max_age=600)
        cn, counter = _make_handler()
        await _run(mw, _scope(headers=[(b'host', b'a.example')]), cn)
        await _run(mw, _scope(headers=[(b'host', b'b.example')]), cn)
        assert counter['n'] == 2

    async def test_case_and_default_port_are_one_origin(self):
        mw = Cache(max_age=600)
        cn, counter = _make_handler()
        await _run(mw, _scope(headers=[(b'host', b'Example.com')]), cn)
        await _run(mw, _scope(headers=[(b'host', b'example.com:080')]), cn)
        assert counter['n'] == 1

    async def test_an_ip_literal_is_not_the_same_name(self):
        mw = Cache(max_age=600)
        cn, counter = _make_handler()
        await _run(mw, _scope(headers=[(b'host', b'[v1.example]')]), cn)
        await _run(mw, _scope(headers=[(b'host', b'v1.example')]), cn)
        assert counter['n'] == 2

    async def test_two_host_fields_bypass_the_cache(self):
        mw = Cache(max_age=600)
        cn, counter = _make_handler()
        await _run(mw, _scope(headers=[(b'host', b'a.example'),
                                       (b'host', b'b.example')]), cn)
        await _run(mw, _scope(headers=[(b'host', b'a.example')]), cn)
        assert counter['n'] == 2

    @pytest.mark.parametrize('authority', [
        b'a.example@b.example', b'a.example/x', b'a.example:invalid',
        b'[::1]suffix', b'',
    ])
    async def test_an_ambiguous_authority_bypasses_the_cache(self, authority):
        mw = Cache(max_age=600)
        cn, counter = _make_handler()
        await _run(mw, _scope(headers=[(b'host', authority)]), cn)
        await _run(mw, _scope(headers=[(b'host', authority)]), cn)
        assert counter['n'] == 2


@pytest.mark.asyncio
class TestCapture:
    """The miss-path buffer on its own: it holds a response until its body is
    known, then either stores it or forwards it in order."""

    def _capture(self, mw):
        sent: list = []

        async def send(event):
            sent.append(event)

        conn = _scope()
        base_key = (conn.method, ('http', 'testserver', 80), conn.path,
                    conn.query_string)
        return _Capture(mw, conn, send, base_key), sent

    @staticmethod
    def _entry(mw):
        bucket = mw._store[('GET', ('http', 'testserver', 80), '/', b'')]
        return bucket.entries[()]

    async def test_a_complete_response_is_stored_and_released_once(self):
        mw = Cache(max_age=600)
        capture, sent = self._capture(mw)
        event = NativeResponse.complete(
            200, [(b'content-type', b'text/plain')], b'hello')
        await capture.send(event)
        await capture.release()
        await capture.release()
        assert sent == [event]
        entry = self._entry(mw)
        assert (entry.status, entry.body) == (200, b'hello')
        assert entry.etag.startswith(b'W/"')
        # The live response carries the same ETag the entry stored.
        assert (b'etag', entry.etag) in event._header

    async def test_the_stored_header_is_a_copy(self):
        mw = Cache(max_age=600)
        capture, _ = self._capture(mw)
        event = NativeResponse.complete(
            200, [(b'content-type', b'text/plain')], b'hello')
        await capture.send(event)
        event.header.append((b'x-injected', b'1'))
        assert (b'x-injected', b'1') not in self._entry(mw).header

    async def test_a_streamed_body_is_passed_through_unstored(self):
        mw = Cache(max_age=600)
        capture, sent = self._capture(mw)
        header = NativeResponse(
            status=200, header=[(b'content-type', b'text/plain')])
        first = NativeResponse(body=b'hel', more_body=True)
        last = NativeResponse(body=b'lo')
        for event in (header, first, last):
            await capture.send(event)
        assert sent == [header, first, last]
        assert not mw._store

    async def test_trailers_are_passed_through_unstored(self):
        mw = Cache(max_age=600)
        capture, sent = self._capture(mw)
        header = NativeResponse(
            status=200, header=[(b'content-type', b'text/plain')],
            body=b'hello', expects_trailers=True)
        trailers = NativeResponse(trailers=[(b'x-sum', b'1')])
        await capture.send(header)
        await capture.send(trailers)
        assert sent == [header, trailers]
        assert not mw._store

    async def test_an_unstorable_response_is_forwarded_at_once(self):
        mw = Cache(max_age=600)
        capture, sent = self._capture(mw)
        header = NativeResponse(
            status=200, header=[(b'cache-control', b'no-store')])
        body = NativeResponse(body=b'hello')
        await capture.send(header)
        assert sent == [header]             # released by the header alone
        await capture.send(body)
        assert sent == [header, body]
        assert not mw._store

    async def test_vary_star_is_forwarded_at_once(self):
        mw = Cache(max_age=600)
        capture, sent = self._capture(mw)
        header = NativeResponse(status=200, header=[(b'vary', b'*')])
        await capture.send(header)
        assert sent == [header]
        assert not mw._store

    async def test_a_response_without_an_etag_is_sent_but_not_stored(self):
        mw = Cache(max_age=600, generate_etag=False)
        capture, sent = self._capture(mw)
        event = NativeResponse.complete(
            200, [(b'content-type', b'text/plain')], b'hello')
        await capture.send(event)
        assert sent == [event]
        assert not mw._store


# ---------------------------------------------------------------------------
# Header-inspection helpers
# ---------------------------------------------------------------------------

class TestHeaderHelpers:
    def test_request_no_store_detection(self):
        assert _must_not_store(b'no-store')
        assert _must_not_store(b'no-cache, no-store')
        assert not _must_not_store(b'no-cache')
        assert not _must_not_store(None)

    def test_an_unreadable_request_field_forbids_storing(self):
        """It could be stating a ``no-store`` this cache failed to read."""
        assert _must_not_store(b'"no-store"')
        assert _must_not_store(b'no-store"')

    def test_directive_names_are_read_in_order(self):
        names = _names(b'public, max-age=300, must-revalidate')
        assert names == [b'public', b'max-age', b'must-revalidate']

    def test_response_max_age_parses_max_age(self):
        assert _stated_max_age([(b'cache-control', b'max-age=120')]) == 120

    def test_response_max_age_prefers_s_maxage(self):
        assert _stated_max_age(
            [(b'cache-control', b'max-age=10, s-maxage=99')]) == 99

    def test_response_max_age_missing_returns_none(self):
        assert _stated_max_age([(b'cache-control', b'public')]) is None
        assert _stated_max_age([]) is None

    def test_response_max_age_garbage_value_ignored(self):
        assert _stated_max_age([(b'cache-control', b'max-age=oops')]) is None

    def test_response_max_age_reads_quoted_and_repeated_values(self):
        assert _stated_max_age([(b'cache-control', b'max-age="0"')]) == 0
        assert _stated_max_age(
            [(b'cache-control', b'max-age=600, max-age=0')]) == 0

    def test_a_field_reads_as_its_directives(self):
        assert _parse_directives(b'public, max-age=300, no-cache="set-cookie"') == [
            (b'public', None), (b'max-age', b'300'),
            (b'no-cache', b'set-cookie')]
        assert _parse_directives(b'x="a\\"b", max-age=600') == [
            (b'x', b'a"b'), (b'max-age', b'600')]

    def test_directive_names_are_case_insensitive(self):
        """RFC 9111 §5.2 compares them that way, so ``Max-Age=0`` is a stated
        zero and not an unstated default."""
        assert _names(b'No-Store, Max-Age=0') == [b'no-store', b'max-age']
        assert _stated_max_age([(b'cache-control', b'Max-Age=60')]) == 60

    def test_whitespace_between_directives_is_allowed(self):
        assert _names(b' public ,\tmax-age=300 ') == [b'public', b'max-age']
        assert _names(b'no-cache,') == [b'no-cache']

    @pytest.mark.parametrize('value', [
        b'max-age="0',              # unterminated quoted-string
        b'max-age="31536000\\"',    # its closing quote is escaped
        b'"no-cache"',              # a quote where a name belongs
        b'"',                       # a quote and nothing else
        b'max-age="0"x',            # text after the closing quote
        b'max-age = 0',             # OWS where the grammar has none
        b'=0',                      # a value with no name
        b'a=b=c',                   # a second '='
        b'no-cache x',              # a name after a name
        b'max-age=',                # '=' with no value
    ])
    def test_a_field_that_breaks_the_grammar_is_unreadable(self, value):
        assert _parse_directives(value) is None
        assert not _readable(value)
        assert _names(value) == []
        assert list(_directives([(b'cache-control', value)])) == []
        assert _smallest([(b'cache-control', value)], b'max-age') is None

    def test_a_malformed_cache_control_falls_back_to_expires(self):
        """Precedence: a ``max-age`` that states no number is unstated, so
        ``Expires`` still decides the lifetime."""
        assert _stated_freshness([
            (b'cache-control', b'max-age=abc'),
            (b'expires', b'Fri, 31 Dec 9999 23:59:59 GMT')]) is not None

    def test_request_no_store_matches_by_name(self):
        assert _must_not_store(b'no-store="a,b"')
        assert not _must_not_store(b'x="a,no-store,b"')

    def test_directives_do_not_split_inside_a_quoted_value(self):
        assert _names(b'x="a,max-age=0", max-age=60') == [b'x', b'max-age']
        assert _smallest(
            [(b'cache-control', b'x="a,max-age=31536000,b"')],
            b'max-age') is None

    def test_read_etag(self):
        assert _response_etag([(b'etag', b'"abc"')]) == b'"abc"'
        assert _response_etag([(b'ETag', b'"abc"')]) == b'"abc"'
        assert _response_etag([]) is None

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
        _, _, body = _split_response(await _run(mw, vscope, cn))  # clean miss
        assert body == b'ENC:br'
        assert counter['n'] == 2, 'handler re-ran; no orphaned stale entry'
        await _run(mw, vscope, cn)                      # now hits again
        assert counter['n'] == 2

    async def test_vary_change_drops_stale_variants(self):
        mw = Cache()
        cn1, first_calls = _vary_handler(vary_value=b'Accept-Encoding')
        await _run(mw, _scope(path='/x', headers=[(b'accept-encoding', b'br')]), cn1)
        assert first_calls['n'] == 1
        # Same URL now varies by a different header → adopt it, drop the stale
        # variant keyed on the old fields.
        cn2, second_calls = _vary_handler(vary_value=b'Accept-Language')
        await _run(mw, _scope(path='/x', headers=[(b'accept-language', b'en')]), cn2)
        assert second_calls['n'] == 1
        # The new language variant hits even when the old varied header changes.
        await _run(mw, _scope(path='/x', headers=[
            (b'accept-language', b'en'), (b'accept-encoding', b'gzip')]), cn2)
        assert second_calls['n'] == 1
        # A request matching only the discarded encoding variant must miss.
        await _run(mw, _scope(path='/x', headers=[(b'accept-encoding', b'br')]), cn2)
        assert second_calls['n'] == 2

    async def test_per_bucket_variant_cap(self):
        from blackbull.middleware.cache import _MAX_VARIANTS_PER_KEY
        mw = Cache()
        cn, counter = _vary_handler()
        for i in range(_MAX_VARIANTS_PER_KEY + 5):
            await _run(mw, _scope(path='/p',
                                  headers=[(b'accept-encoding', f'enc{i}'.encode())]), cn)
        admitted = counter['n']
        await _run(mw, _scope(path='/p', headers=[
            (b'accept-encoding', f'enc{_MAX_VARIANTS_PER_KEY + 4}'.encode())]), cn)
        assert counter['n'] == admitted
        await _run(mw, _scope(path='/p', headers=[(b'accept-encoding', b'enc0')]), cn)
        assert counter['n'] == admitted + 1


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
    def test_vary_fields_absent(self):
        from blackbull.middleware.cache import _vary_fields
        assert _vary_fields([(b'content-type', b'text/plain')]) == ()

    def test_vary_fields_sorted_lowercased(self):
        from blackbull.middleware.cache import _vary_fields
        assert _vary_fields(
            [(b'vary', b'Accept-Encoding, Accept-Language')]
        ) == (b'accept-encoding', b'accept-language')

    def test_vary_star_is_none(self):
        from blackbull.middleware.cache import _vary_fields
        assert _vary_fields([(b'vary', b'*')]) is None

    def test_variant_key_pulls_request_values(self):
        from blackbull.headers import Headers
        from blackbull.middleware.cache import _variant_key
        req = Headers([(b'accept-encoding', b'br')])
        assert _variant_key((b'accept-encoding',), req) == (b'br',)

    def test_variant_key_missing_header_is_empty(self):
        from blackbull.headers import Headers
        from blackbull.middleware.cache import _variant_key
        assert _variant_key((b'accept-encoding',), Headers([])) == (b'',)


# ---------------------------------------------------------------------------
# Replay isolation — the guard the dict form used to provide by copying
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestReplayIsolation:
    """A cache hit must hand out a private copy of the stored response.

    Middleware below the cache — CORS, the route header injector — append to
    a response's header list **in place**.  The dict-era cache copied every
    event on replay for exactly this reason; storing native objects has to
    keep that guarantee, or each hit grows the stored entry by one header and
    the response drifts on every request.
    """

    async def test_downstream_header_append_does_not_reach_the_entry(self):
        mw = Cache()
        cn, _ = _make_handler()

        async def _hit():
            sent: list = []

            async def send(event):
                # Simulate CORS / _inject_response_headers: in-place append.
                if isinstance(event, NativeResponse) and event._header is not None:
                    event.header.append((b'x-injected', b'1'))
                sent.append(event)

            await mw(_scope(), None, send, cn)
            return sent

        await _hit()                      # miss → stores
        first = await _hit()              # hit  → replay
        second = await _hit()             # hit  → replay again

        def _count(events, name):
            n = 0
            for e in events:
                if isinstance(e, NativeResponse) and e._header is not None:
                    n += sum(1 for k, _ in e._header if k.lower() == name)
            return n

        assert _count(first, b'x-injected') == 1
        assert _count(second, b'x-injected') == 1, (
            'the injected header accumulated in the stored entry — replay '
            'handed out the cached object instead of a copy')

    async def test_body_and_status_stable_across_hits(self):
        mw = Cache()
        cn, _ = _make_handler()

        await _run(mw, _scope(), cn)
        a = _split_response(await _run(mw, _scope(), cn))
        b = _split_response(await _run(mw, _scope(), cn))

        assert a[0] == b[0]
        assert a[2] == b[2]
