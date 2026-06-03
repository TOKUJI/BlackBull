"""Unit tests for ProtocolBuffer — the cancellable byte buffer at the
heart of Sprint 30 Tier 1.5's custom asyncio protocol.

The buffer replaces ``asyncio.StreamReader`` on the keepalive-idle
read path.  Tests cover:

- Feed-then-read sequences (data already buffered, data after feed).
- EOF semantics (read what's left, then raise IncompleteReadError).
- read_until: delimiter found mid-buffer, across-feed boundaries,
  EOF before delimiter, max_size overflow.
- read_exactly: short reads, exact reads, EOF mid-read.
- read: partial reads, EOF returns b''.
- Cancellation safety: suspended consumer can be cancelled; buffer
  recovers for a fresh reader.
- Concurrent-suspend guard: second suspend while another is active
  raises RuntimeError (test-only invariant; production has one
  consumer per buffer).

No asyncio.Server involved — tests run against the bare class so
they exercise the buffer's logic without transport-layer noise.
"""
from __future__ import annotations

import asyncio

import pytest

from blackbull.server.protocol_buffer import LimitOverrunError, ProtocolBuffer
from blackbull.server.recipient import IncompleteReadError


# ---------------------------------------------------------------------------
# read_exactly
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_read_exactly_data_already_buffered():
    """When the buffer already has >= n bytes, read_exactly returns
    immediately without suspending."""
    buf = ProtocolBuffer()
    buf.feed(b'abcdefghij')
    assert await buf.read_exactly(5) == b'abcde'
    # Remaining bytes still available.
    assert await buf.read_exactly(5) == b'fghij'


@pytest.mark.asyncio
async def test_read_exactly_waits_for_more_data():
    """When the buffer has fewer than n bytes, the call suspends
    until enough data arrives via feed()."""
    buf = ProtocolBuffer()
    buf.feed(b'abc')

    async def feed_more():
        await asyncio.sleep(0)   # let the consumer suspend first
        buf.feed(b'defgh')

    task = asyncio.create_task(buf.read_exactly(5))
    asyncio.create_task(feed_more())
    result = await task
    assert result == b'abcde'


@pytest.mark.asyncio
async def test_read_exactly_eof_before_n_bytes_raises_incomplete():
    """If EOF arrives before n bytes are available, IncompleteReadError
    is raised with .partial set to whatever was buffered."""
    buf = ProtocolBuffer()
    buf.feed(b'ab')
    buf.feed_eof()
    with pytest.raises(IncompleteReadError) as info:
        await buf.read_exactly(10)
    assert info.value.partial == b'ab'        # type: ignore[attr-defined]
    assert info.value.expected == 10          # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_read_exactly_zero_returns_empty_without_waiting():
    """n=0 is a no-op fast path — does not suspend even on an empty
    buffer."""
    buf = ProtocolBuffer()
    assert await buf.read_exactly(0) == b''


@pytest.mark.asyncio
async def test_read_exactly_negative_n_raises():
    buf = ProtocolBuffer()
    with pytest.raises(ValueError, match='non-negative'):
        await buf.read_exactly(-1)


# ---------------------------------------------------------------------------
# read_until
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_read_until_finds_sep_in_buffer():
    buf = ProtocolBuffer()
    buf.feed(b'GET /healthz HTTP/1.1\r\nHost: x\r\n\r\nbody')
    line = await buf.read_until(b'\r\n')
    assert line == b'GET /healthz HTTP/1.1\r\n'
    # Remaining data still in buffer.
    rest = await buf.read(1024)
    assert rest == b'Host: x\r\n\r\nbody'


@pytest.mark.asyncio
async def test_read_until_finds_sep_after_feed():
    """Suspended read_until wakes up when sep arrives across multiple
    feeds."""
    buf = ProtocolBuffer()
    buf.feed(b'partial line ')

    async def complete():
        await asyncio.sleep(0)
        buf.feed(b'with delim\r\n')

    task = asyncio.create_task(buf.read_until(b'\r\n'))
    asyncio.create_task(complete())
    line = await task
    assert line == b'partial line with delim\r\n'


@pytest.mark.asyncio
async def test_read_until_sep_straddles_two_feeds():
    """sep is multi-byte and the boundary falls between feeds — the
    buffer's scan-from optimization must not miss it."""
    buf = ProtocolBuffer()
    buf.feed(b'abc\r')           # first half of delimiter
    async def feed_rest():
        await asyncio.sleep(0)
        buf.feed(b'\ndef')        # second half completes \r\n
    task = asyncio.create_task(buf.read_until(b'\r\n'))
    asyncio.create_task(feed_rest())
    result = await task
    assert result == b'abc\r\n'


@pytest.mark.asyncio
async def test_read_until_eof_before_sep_raises_incomplete():
    buf = ProtocolBuffer()
    buf.feed(b'no delimiter here')
    buf.feed_eof()
    with pytest.raises(IncompleteReadError) as info:
        await buf.read_until(b'\r\n')
    assert info.value.partial == b'no delimiter here'   # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_read_until_max_size_overflow_raises():
    """If max_size is exceeded before sep is found, LimitOverrunError
    is raised with .consumed set to the buffer length."""
    buf = ProtocolBuffer()
    big = b'a' * 100
    buf.feed(big)
    with pytest.raises(LimitOverrunError) as info:
        await buf.read_until(b'\r\n', max_size=50)
    assert info.value.consumed >= 50


@pytest.mark.asyncio
async def test_read_until_empty_sep_raises():
    buf = ProtocolBuffer()
    with pytest.raises(ValueError, match='non-empty'):
        await buf.read_until(b'')


# ---------------------------------------------------------------------------
# read
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_read_returns_everything_when_n_negative():
    buf = ProtocolBuffer()
    buf.feed(b'hello world')
    assert await buf.read(-1) == b'hello world'
    # And subsequent reads (no more data, no EOF yet) would suspend.


@pytest.mark.asyncio
async def test_read_returns_partial_when_n_smaller_than_buffer():
    buf = ProtocolBuffer()
    buf.feed(b'abcdef')
    assert await buf.read(3) == b'abc'
    assert await buf.read(3) == b'def'


@pytest.mark.asyncio
async def test_read_returns_empty_on_eof():
    buf = ProtocolBuffer()
    buf.feed_eof()
    assert await buf.read(100) == b''


@pytest.mark.asyncio
async def test_read_drains_remaining_then_returns_empty_on_eof():
    buf = ProtocolBuffer()
    buf.feed(b'last')
    buf.feed_eof()
    assert await buf.read(100) == b'last'
    assert await buf.read(100) == b''


# ---------------------------------------------------------------------------
# Cancellation
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_suspended_read_can_be_cancelled():
    """If the consumer task is cancelled while parked, the cancellation
    propagates cleanly and the buffer is reusable afterwards."""
    buf = ProtocolBuffer()
    task = asyncio.create_task(buf.read_exactly(100))
    await asyncio.sleep(0)   # let it suspend
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        # Await raises CancelledError (captured by pytest.raises);
        # bind to ``_`` so CodeQL's "ineffectual statement" rule
        # doesn't trip on the discarded-await pattern.
        _ = await task
    # Buffer's waiter slot is released; a fresh consumer can read.
    buf.feed(b'recovered')
    # Note: we can't readuntil for sep that's not there without another
    # suspend; just confirm the buffer state is sane.
    assert buf.buffered_bytes == len(b'recovered')


@pytest.mark.asyncio
async def test_concurrent_suspend_is_rejected():
    """Spawning a second consumer task while the first is suspended
    raises RuntimeError — surfaces misuse during dev/tests.  Production
    contract: one consumer per buffer."""
    buf = ProtocolBuffer()
    first = asyncio.create_task(buf.read_exactly(10))
    await asyncio.sleep(0)   # let first suspend
    second = asyncio.create_task(buf.read_exactly(5))
    with pytest.raises(RuntimeError, match='concurrent suspends'):
        _ = await second                   # raises (captured)
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        _ = await first                    # raises (captured)


# ---------------------------------------------------------------------------
# close() — force-close path
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_close_wakes_pending_consumer_with_incomplete_read():
    """ProtocolBuffer.close() marks the buffer as eof and wakes any
    suspended consumer.  Pending reads then raise IncompleteReadError
    (since no data was buffered)."""
    buf = ProtocolBuffer()
    task = asyncio.create_task(buf.read_exactly(10))
    await asyncio.sleep(0)
    buf.close()
    with pytest.raises(IncompleteReadError):
        _ = await task                     # raises (captured)


@pytest.mark.asyncio
async def test_feed_after_close_is_dropped():
    """Defensive: if the protocol erroneously calls feed() after
    eof/close, the bytes are silently dropped (logged at DEBUG)."""
    buf = ProtocolBuffer()
    buf.close()
    buf.feed(b'too late')
    assert buf.buffered_bytes == 0


@pytest.mark.asyncio
async def test_feed_eof_is_idempotent():
    buf = ProtocolBuffer()
    buf.feed_eof()
    buf.feed_eof()
    assert buf.eof_received is True


# ---------------------------------------------------------------------------
# Mixed sequences (realistic HTTP/1.1 flow)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_http1_request_then_eof():
    """Realistic shape: receive a full HTTP/1.1 request, then EOF.

    Mirrors what http1_actor.run() does — readuntil for headers,
    optionally readexactly for body, then next-iteration readuntil
    that hits EOF."""
    buf = ProtocolBuffer()
    request = (b'GET /static/app.js HTTP/1.1\r\n'
               b'Host: localhost\r\n'
               b'Accept-Encoding: br\r\n'
               b'\r\n')
    buf.feed(request)
    buf.feed_eof()

    # Request line.
    rline = await buf.read_until(b'\r\n')
    assert rline == b'GET /static/app.js HTTP/1.1\r\n'
    # Headers up to and including CRLFCRLF.
    rest = await buf.read_until(b'\r\n\r\n')
    assert rest == b'Host: localhost\r\nAccept-Encoding: br\r\n\r\n'
    # Next-iteration readuntil hits EOF.
    with pytest.raises(IncompleteReadError):
        await buf.read_until(b'\r\n')


@pytest.mark.asyncio
async def test_http1_pipelined_requests_drain_in_order():
    """Two HTTP/1.1 requests fed together drain in order without
    confusing read_until's scan."""
    buf = ProtocolBuffer()
    buf.feed(b'GET /a HTTP/1.1\r\n\r\nGET /b HTTP/1.1\r\n\r\n')

    first = await buf.read_until(b'\r\n\r\n')
    assert first == b'GET /a HTTP/1.1\r\n\r\n'
    second = await buf.read_until(b'\r\n\r\n')
    assert second == b'GET /b HTTP/1.1\r\n\r\n'
