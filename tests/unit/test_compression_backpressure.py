"""Unit tests for the Compression middleware's executor backpressure
(Sprint 29 #3).

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

    async def send(event: dict) -> None:
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

    Regression for the Sprint 29 bug where the outer code path forwarded a
    second start+empty-body pair after the skip-path had already inline-
    forwarded the real response, producing two start events on the same
    response and causing the HTTP/1.1 sender to close the connection after
    every successful response (50 % read-error rate under wrk keep-alive)."""
    mw = Compression()

    events: list[dict] = []

    async def send(event: dict) -> None:
        events.append(event)

    async def upstream(scope, receive, send_):
        # Upstream sets Content-Encoding itself — typical precompressed
        # static-file serving (Sprint 29 #1).
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
# Sprint 33 — pass-through fast path + Accept-Encoding selection cache
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_passthrough_skips_event_reparsing(monkeypatch):
    """When the inner handler already sets ``Content-Encoding`` (e.g.
    StaticFiles serving a precompressed sibling), the middleware's
    ``intercepting_send`` must forward subsequent body events verbatim —
    no second ``parse_response_event`` call.

    This is what gives the static-file cache-hit path its Sprint 33 win.
    Without the fast path, ``parse_response_event`` runs once per ASGI
    event; the precompressed-sibling response is 2 events so the cost
    doubles for no useful work."""
    from blackbull.middleware import compression as _compression

    calls: list[bytes] = []
    real_parse = _compression.parse_response_event

    def counting_parse(event):
        calls.append(event.get('type', b''))
        return real_parse(event)

    monkeypatch.setattr(_compression, 'parse_response_event', counting_parse)

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
        events.append(event)

    async def call_next(scope, receive, send):
        await already_encoded_handler(scope, receive, send)

    mw = Compression()
    await mw(_scope(b'br, gzip'), _noop_receive, out_send, call_next)

    # The start event must be parsed (we need to inspect Content-Encoding
    # to decide to skip).  The body event must NOT be parsed — that's the
    # fast path.
    assert 'http.response.start' in calls, \
        f'expected start to be parsed; got {calls!r}'
    assert 'http.response.body' not in calls, \
        f'fast path bypassed: body event re-parsed; got {calls!r}'

    # Sanity: response still round-trips correctly.
    starts = [e for e in events if e['type'] == 'http.response.start']
    bodies = [e for e in events if e['type'] == 'http.response.body']
    assert len(starts) == 1 and len(bodies) == 1
    assert bodies[0]['body'] == b'BR-COMPRESSED-BYTES'


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
