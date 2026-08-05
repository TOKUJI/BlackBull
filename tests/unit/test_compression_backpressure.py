"""Unit tests for the Compression middleware's executor backpressure
Backpressure on the compression path.

When the asyncio executor already has ``executor_max_inflight`` compressions
running, the middleware skips compression on additional eligible responses
and serves them uncompressed.  Prevents unbounded executor-queue growth that
caused the HttpArena ``static`` profile to collapse to 0 r/s under burst load.
"""
from __future__ import annotations

import asyncio
import gzip

import pytest

from blackbull.middleware.compression import Compression


def _scope(accept_encoding: bytes = b'gzip'):
    from blackbull.connection import Connection
    return Connection.from_scope({
        'type': 'http',
        'method': 'GET',
        'path': '/',
        'headers': [(b'accept-encoding', accept_encoding)],
    })


async def _noop_receive():
    return {'type': 'http.disconnect'}


def _make_handler(body: bytes, content_type: bytes = b'text/plain'):
    """Build a fake inner handler that yields a single non-streaming response."""

    async def handler(scope, receive, send):
        await send({
            'type': 'http.response.start',
            'status': 200,
            'headers': [
                (b'content-type', content_type),
                (b'content-length', str(len(body)).encode()),
            ],
        })
        await send({'type': 'http.response.body', 'body': body, 'more_body': False})

    return handler


async def _run_through(mw, body: bytes, accept: bytes = b'gzip') -> dict:
    """Run *mw* (a Compression instance, post-as_middleware) end-to-end and
    return ``{'headers': {bytes: bytes}, 'body': bytes}``."""
    events: list[dict] = []

    async def send(event) -> None:
        # The middleware is ``@as_middleware``-decorated, so it emits the H1
        # native contract (NativeResponse).  These tests assert on the ASGI
        # event shape, so the seam is normalised away here — the same
        # convention ``test_middlewares._collect`` uses.
        from blackbull.native import NativeResponse
        if isinstance(event, NativeResponse):
            events.extend(event.to_asgi())
        else:
            events.append(event)

    handler = _make_handler(body)

    async def call_next(scope, receive, send):
        await handler(scope, receive, send)

    await mw(_scope(accept), _noop_receive, send, call_next)
    start = next(e for e in events if e['type'] == 'http.response.start')
    body_out = b''.join(e.get('body', b'')
                        for e in events if e['type'] == 'http.response.body')
    return {
        'status': start['status'],
        'headers': dict(start['headers']),
        'body': body_out,
    }


# A body large enough to cross the default executor_threshold so the
# offload codepath actually runs.  Repetitive content so gzip is effective.
_BIG_BODY = (b'lorem ipsum dolor sit amet ' * 4000)   # ~104 KB


@pytest.mark.asyncio
async def test_compression_runs_when_under_inflight_cap():
    """A single offload below the cap behaves like the existing
    (compress + Content-Encoding) path."""
    mw = Compression(executor_max_inflight=4)
    res = await _run_through(mw, _BIG_BODY, accept=b'gzip')
    assert res['status'] == 200
    assert res['headers'].get(b'content-encoding') == b'gzip'
    # Body is compressed
    assert len(res['body']) < len(_BIG_BODY)
    assert gzip.decompress(res['body']) == _BIG_BODY


@pytest.mark.asyncio
async def test_backpressure_serves_uncompressed_when_inflight_at_cap():
    """When `_executor_inflight` is already at the cap, the middleware
    must serve the body uncompressed (no Content-Encoding, original length)."""
    mw = Compression(executor_max_inflight=1)
    # Pre-fill the counter so the next request sees a full pool.
    mw._executor_inflight = 1
    res = await _run_through(mw, _BIG_BODY, accept=b'gzip')
    assert res['status'] == 200
    assert b'content-encoding' not in res['headers']
    # Body is exactly the original — no compression happened.
    assert res['body'] == _BIG_BODY


@pytest.mark.asyncio
async def test_max_inflight_zero_disables_backpressure():
    """`executor_max_inflight=0` reverts to the pre-0.29 behaviour:
    unbounded queueing.  Even with a huge inflight counter, the offload
    still runs and the response is compressed."""
    mw = Compression(executor_max_inflight=0)
    mw._executor_inflight = 9999      # arbitrarily large
    res = await _run_through(mw, _BIG_BODY, accept=b'gzip')
    assert res['headers'].get(b'content-encoding') == b'gzip'


@pytest.mark.asyncio
async def test_counter_decrements_after_successful_compression():
    """The inflight counter must return to 0 after a successful offload —
    otherwise the cap would tighten on every request."""
    mw = Compression(executor_max_inflight=4)
    assert mw._executor_inflight == 0
    await _run_through(mw, _BIG_BODY, accept=b'gzip')
    assert mw._executor_inflight == 0


@pytest.mark.asyncio
async def test_counter_decrements_even_on_executor_exception(monkeypatch):
    """If the executor itself raises, the inflight counter must still
    decrement.  Otherwise one bad compress would permanently lower the cap."""
    mw = Compression(executor_max_inflight=4)

    class Boom(Exception):
        pass

    def bad_compressor(_body):
        raise Boom("compressor exploded")

    # Replace gzip with the failing compressor.
    mw._available['gzip'] = bad_compressor

    async def call_next(scope, receive, send):
        await _make_handler(_BIG_BODY)(scope, receive, send)

    async def send(event):
        pass

    with pytest.raises(Boom):
        await mw(_scope(b'gzip'), _noop_receive, send, call_next)

    assert mw._executor_inflight == 0


@pytest.mark.asyncio
async def test_skip_path_emits_exactly_one_start_event():
    """When the upstream layer already set Content-Encoding (e.g. StaticFiles
    serving a precompressed sibling), the middleware must emit **one**
    response.start event and **one** body event — not duplicate them.

    Regression for the bug where the outer code path forwarded a
    second start+empty-body pair after the skip-path had already inline-
    forwarded the real response, producing two start events on the same
    response and causing the HTTP/1.1 sender to close the connection after
    every successful response (50 % read-error rate under wrk keep-alive)."""
    mw = Compression()

    events: list[dict] = []

    async def send(event) -> None:
        # Normalise the native seam away — these assertions are about how
        # many response.start events reach the wire, which is a property of
        # the ASGI shape either representation expands to.
        from blackbull.native import NativeResponse
        if isinstance(event, NativeResponse):
            events.extend(event.to_asgi())
        else:
            events.append(event)

    async def upstream(scope, receive, send_):
        # Upstream sets Content-Encoding itself — typical precompressed
        # static-file serving.
        await send_({
            'type': 'http.response.start',
            'status': 200,
            'headers': [
                (b'content-type', b'text/javascript'),
                (b'content-encoding', b'br'),
                (b'content-length', b'52037'),
            ],
        })
        await send_({'type': 'http.response.body',
                     'body': b'BR-COMPRESSED-BYTES', 'more_body': False})

    async def call_next(scope, receive, send_):
        await upstream(scope, receive, send_)

    await mw(_scope(b'br'), _noop_receive, send, call_next)

    starts = [e for e in events if e['type'] == 'http.response.start']
    bodies = [e for e in events if e['type'] == 'http.response.body']

    assert len(starts) == 1, f'expected exactly 1 start event, got {len(starts)}'
    assert len(bodies) == 1, f'expected exactly 1 body event, got {len(bodies)}'
    # Original upstream-set encoding survived end-to-end (no double-wrap).
    assert dict(starts[0]['headers']).get(b'content-encoding') == b'br'
    assert bodies[0]['body'] == b'BR-COMPRESSED-BYTES'
    assert bodies[0]['more_body'] is False


@pytest.mark.asyncio
async def test_small_body_under_threshold_ignores_inflight_cap():
    """Bodies below `executor_threshold` compress on the event loop
    (no executor offload), so the inflight cap shouldn't gate them."""
    mw = Compression(executor_max_inflight=1, executor_threshold=999_999)
    mw._executor_inflight = 1   # would block the offload path
    small = b'lorem ipsum ' * 100  # ~1200 bytes, well under 999_999
    res = await _run_through(mw, small, accept=b'gzip')
    # Compression still happens — it's on the event loop, no executor offload.
    assert res['headers'].get(b'content-encoding') == b'gzip'


# ---------------------------------------------------------------------------
# Pass-through fast path + Accept-Encoding selection cache
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_passthrough_skips_event_reparsing(monkeypatch):
    """When the inner handler already sets ``Content-Encoding`` (e.g.
    StaticFiles serving a precompressed sibling), the response must reach the
    wire without being taken apart on the way.

    The single-world seam removes both costs the static cache-hit path used to
    pay per request: dict re-parsing and native expansion.  The first is now
    structural — ``parse_response_event`` is not reachable from this module at
    all — so it is asserted as absence of the import rather than a call count.
    The second is counted."""
    from blackbull.middleware import compression as _compression
    from blackbull.native import NativeResponse

    assert not hasattr(_compression, 'parse_response_event'), (
        'the dict lane is back: compression imported parse_response_event')

    expansions = {'n': 0}
    real_to_asgi = NativeResponse.to_asgi

    def counting_to_asgi(self):
        expansions['n'] += 1
        return real_to_asgi(self)

    monkeypatch.setattr(NativeResponse, 'to_asgi', counting_to_asgi)

    async def already_encoded_handler(scope, receive, send):
        await send({
            'type': 'http.response.start',
            'status': 200,
            'headers': [
                (b'content-type', b'text/javascript'),
                (b'content-encoding', b'br'),
                (b'content-length', b'19'),
            ],
        })
        await send({'type': 'http.response.body',
                    'body': b'BR-COMPRESSED-BYTES', 'more_body': False})

    events: list[dict] = []

    async def out_send(event):
        if isinstance(event, NativeResponse):
            events.extend(real_to_asgi(event))
        else:
            events.append(event)

    async def call_next(scope, receive, send):
        await already_encoded_handler(scope, receive, send)

    mw = Compression()
    await mw(_scope(b'br, gzip'), _noop_receive, out_send, call_next)

    assert expansions['n'] == 0, \
        (f'response expanded through to_asgi() {expansions["n"]}x — the native '
         f'path round-tripped it back into dicts')

    # Sanity: response still round-trips correctly, unchanged.
    starts = [e for e in events if e['type'] == 'http.response.start']
    bodies = [e for e in events if e['type'] == 'http.response.body']
    assert len(starts) == 1 and len(bodies) == 1
    assert bodies[0]['body'] == b'BR-COMPRESSED-BYTES'
    assert dict(starts[0]['headers'])[b'content-encoding'] == b'br', \
        'the precompressed sibling must not be re-encoded'


@pytest.mark.asyncio
async def test_codec_selection_cache_returns_same_result_on_repeat():
    """Same ``Accept-Encoding`` header → same selection.  The cache must
    not affect correctness; it just avoids re-parsing q-values on every
    request when clients send a constant header (browsers, benchmarks)."""
    mw = Compression()
    header = b'br;q=1, gzip;q=0.8'
    first = mw._select_codec(header)
    second = mw._select_codec(header)
    third = mw._select_codec(header)
    assert first == second == third
    assert first is not None and first[0] == 'br'
    # Different header → different cache entry (still correct).
    gzip_only = mw._select_codec(b'gzip')
    assert gzip_only is not None and gzip_only[0] == 'gzip'


@pytest.mark.asyncio
async def test_codec_selection_cache_bounded():
    """The cache must not grow unboundedly under hostile-peer load that
    rotates Accept-Encoding header values."""
    mw = Compression()
    for i in range(300):
        mw._select_codec(f'gzip;q=0.{i:03d}'.encode())
    assert len(mw._codec_cache) <= 256, \
        f'cache exceeded bound: {len(mw._codec_cache)}'


# ---------------------------------------------------------------------------
# Native complete-response path (Sprint 94 — native 直結化)
# ---------------------------------------------------------------------------

def _make_native_handler(body: bytes, content_type: bytes = b'text/plain',
                         content_encoding: bytes | None = None):
    """Build a fake inner handler that yields ONE complete
    ``NativeResponse`` (header + terminal body in one object) — the exact
    shape the router emits on the native H1 path (``Response.to_native()``).

    ``intercepting_send`` must consume this object directly (no
    ``to_asgi()`` expansion) — that is the regression fix under test.
    """
    from blackbull.native import NativeResponse

    headers = [(b'content-type', content_type)]
    if content_encoding is not None:
        headers.append((b'content-encoding', content_encoding))
    headers.append((b'content-length', str(len(body)).encode()))

    async def handler(scope, receive, send):
        await send(NativeResponse(status=200, header=headers, body=body))

    return handler


async def _run_native_through(mw, body: bytes, accept: bytes = b'gzip',
                              content_type: bytes = b'text/plain',
                              content_encoding: bytes | None = None) -> list:
    """Run *mw* with a complete-NativeResponse inner handler; return the raw
    downstream event list (asserted to be exactly one NativeResponse)."""
    from blackbull.native import NativeResponse

    events: list = []

    async def send(event) -> None:
        events.append(event)

    handler = _make_native_handler(body, content_type, content_encoding)

    async def call_next(scope, receive, send):
        await handler(scope, receive, send)

    await mw(_scope(accept), _noop_receive, send, call_next)
    return events


@pytest.mark.asyncio
async def test_native_complete_response_emits_one_native_response():
    """The complete-NativeResponse path consumes the object directly: the
    downstream send receives exactly ONE ``NativeResponse`` — not the two
    ASGI dicts a ``to_asgi()`` expansion would emit.  Compressed body with
    content-encoding, rewritten content-length, and Vary.

    This is the Sprint 94 regression pin: expanding through ``to_asgi()``
    round-tripped NR → dict → NR on every request (static −3.4〜−6.4 %,
    json-comp −1.2〜−3.2 % vs v0.67.0)."""
    from blackbull.native import NativeResponse

    mw = Compression()
    body = b'hello world, hello world ' * 30   # ~660 B, > min_size
    events = await _run_native_through(mw, body, accept=b'gzip')

    assert len(events) == 1, \
        f'expected exactly 1 downstream object, got {len(events)}'
    assert isinstance(events[0], NativeResponse), \
        f'expected a NativeResponse, got {type(events[0])!r}'
    nr = events[0]
    assert nr._body != body            # compressed
    assert gzip.decompress(nr._body) == body
    hdrs = dict(nr._header)
    assert hdrs[b'content-encoding'] == b'gzip'
    assert hdrs[b'content-length'] == str(len(nr._body)).encode()
    assert b'accept-encoding' in hdrs.get(b'vary', b'').lower()


@pytest.mark.asyncio
async def test_native_complete_pre_encoded_passes_through():
    """A pre-encoded complete NativeResponse (the static-asset case —
    StaticFiles serving a precompressed sibling) passes through verbatim:
    one object, no re-compression, no Vary stamp."""
    from blackbull.native import NativeResponse

    mw = Compression()
    body = b'BR-COMPRESSED-BYTES'
    events = await _run_native_through(
        mw, body, accept=b'br, gzip', content_encoding=b'br')

    assert len(events) == 1
    assert isinstance(events[0], NativeResponse)
    nr = events[0]
    assert nr._body == body            # verbatim — no double-wrap
    hdrs = dict(nr._header)
    assert hdrs[b'content-encoding'] == b'br'
    assert b'vary' not in hdrs         # pre-encoded → no Vary stamp


@pytest.mark.asyncio
async def test_native_complete_small_body_uncompressed_with_vary():
    """A compressible but below-min-size complete NativeResponse passes
    through uncompressed, yet still carries Vary: Accept-Encoding — the
    shared-cache correctness the decision-point stamp guarantees."""
    from blackbull.native import NativeResponse

    mw = Compression()
    body = b'tiny'                     # < _MIN_SIZE (100)
    events = await _run_native_through(mw, body, accept=b'gzip')

    assert len(events) == 1
    assert isinstance(events[0], NativeResponse)
    nr = events[0]
    assert nr._body == body            # uncompressed
    assert dict(nr._header).get(b'content-encoding') is None
    assert b'accept-encoding' in dict(nr._header).get(b'vary', b'').lower()


@pytest.mark.asyncio
async def test_native_complete_at_cap_serves_uncompressed_with_vary():
    """The native executor-at-cap exit: when ``_executor_inflight`` is already
    at the cap, a complete compressible NativeResponse is served uncompressed
    (one object, no content-encoding, content-length untouched) — but Vary is
    still stamped, because the decision-point stamp precedes the backpressure
    check.  Pins the 'every exit path carries the cache key' invariant on the
    native lane."""
    from blackbull.native import NativeResponse

    mw = Compression(executor_max_inflight=1)
    mw._executor_inflight = 1          # full pool → backpressure kicks in
    body = b'lorem ipsum dolor sit amet ' * 4000   # ~104 KB, > threshold
    events = await _run_native_through(mw, body, accept=b'gzip')

    assert len(events) == 1
    assert isinstance(events[0], NativeResponse)
    nr = events[0]
    assert nr._body == body            # verbatim — uncompressed
    hdrs = dict(nr._header)
    assert b'content-encoding' not in hdrs
    assert hdrs[b'content-length'] == str(len(body)).encode()
    assert b'accept-encoding' in hdrs.get(b'vary', b'').lower()
