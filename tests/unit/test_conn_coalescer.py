"""Unit tests for ConnCoalescer — connection-level TCP segment coalescing
(Sprint 59, connection-level-tcp-segment-coalescing).

The coalescer batches per-stream HTTP/2 frame writes so N streams completing
within a short window flush as one TCP segment.  Invariants under test:

* Disabled (hold_us=0) is a transparent pass-through — every write goes
  straight to the transport, byte-for-byte, in call order.
* The first frame of an idle window writes immediately (no added latency for
  an isolated response); frames arriving while a window is open are held.
* Held frames flush in FIFO order (the HPACK/wire-order invariant) via the
  timer, an explicit flush(), or a byte threshold.
* Peer-close tolerant: once the transport is gone, further writes drop.
"""
from __future__ import annotations

import asyncio

import pytest

from blackbull.server.sender import AbstractWriter, ConnCoalescer


class _RecordingWriter(AbstractWriter):
    """AbstractWriter double that records each write() as one entry.

    One list entry per write() call, so tests can assert *how many* TCP
    segments would be produced (the whole point of coalescing), not just the
    concatenated bytes.
    """

    def __init__(self, fail: type[BaseException] | None = None):
        self.writes: list[bytes] = []
        self._fail = fail

    async def write(self, data: bytes) -> None:
        if self._fail is not None:
            raise self._fail('transport gone')
        self.writes.append(data)


@pytest.mark.asyncio
async def test_disabled_is_passthrough():
    """hold_us=0 → every write goes direct, in order, one segment each."""
    w = _RecordingWriter()
    c = ConnCoalescer(w, hold_us=0)
    assert c.enabled is False
    await c.write(b'A')
    await c.write(b'B')
    await c.write(b'C')
    assert w.writes == [b'A', b'B', b'C']


@pytest.mark.asyncio
async def test_first_frame_of_window_writes_immediately():
    """An isolated response pays no added latency: the first frame in an
    idle window is written straight through."""
    w = _RecordingWriter()
    c = ConnCoalescer(w, hold_us=50_000)  # 50ms — long enough not to fire mid-test
    assert c.enabled is True
    await c.write(b'first')
    assert w.writes == [b'first']


@pytest.mark.asyncio
async def test_subsequent_frames_are_held_then_flushed_fifo():
    """Frames arriving while a window is open are buffered and flushed as a
    single segment, in FIFO (HPACK/wire) order."""
    w = _RecordingWriter()
    c = ConnCoalescer(w, hold_us=50_000)
    await c.write(b'H1')   # immediate — opens the window
    await c.write(b'D1')   # held
    await c.write(b'H2')   # held
    assert w.writes == [b'H1']          # nothing flushed yet
    await c.flush()
    assert w.writes == [b'H1', b'D1H2']  # one coalesced segment, order preserved


@pytest.mark.asyncio
async def test_timer_flushes_the_batch():
    """With no explicit flush, the hold timer flushes the held batch."""
    w = _RecordingWriter()
    c = ConnCoalescer(w, hold_us=5_000)  # 5ms
    await c.write(b'A')   # immediate
    await c.write(b'B')   # held
    await c.write(b'C')   # held
    assert w.writes == [b'A']
    await asyncio.sleep(0.05)  # let the 5ms timer fire
    assert w.writes == [b'A', b'BC']


@pytest.mark.asyncio
async def test_byte_threshold_forces_early_flush():
    """A batch that reaches the byte threshold flushes without waiting for
    the timer (bounds buffer memory under a large-response burst)."""
    w = _RecordingWriter()
    c = ConnCoalescer(w, hold_us=50_000, threshold=8)
    await c.write(b'A')        # immediate, opens window
    await c.write(b'1234')     # held (4 < 8)
    assert w.writes == [b'A']
    await c.write(b'5678')     # held → total 8 >= threshold → flush
    assert w.writes == [b'A', b'12345678']


@pytest.mark.asyncio
async def test_new_window_after_flush_writes_immediately_again():
    """After a flush the next write opens a fresh window and is immediate —
    a slowly-paced stream (e.g. human-paced SSE) is never held."""
    w = _RecordingWriter()
    c = ConnCoalescer(w, hold_us=5_000)
    await c.write(b'A')   # immediate
    await c.write(b'B')   # held
    await asyncio.sleep(0.05)  # timer flush → [A, B]
    await c.write(b'C')   # fresh window → immediate again
    assert w.writes == [b'A', b'B', b'C']


@pytest.mark.asyncio
async def test_flush_on_empty_buffer_is_noop():
    w = _RecordingWriter()
    c = ConnCoalescer(w, hold_us=50_000)
    await c.flush()
    assert w.writes == []
    await c.write(b'only')   # immediate
    await c.flush()          # nothing buffered
    assert w.writes == [b'only']


@pytest.mark.asyncio
async def test_peer_close_is_tolerated_and_drops_further_writes():
    """A transport that raises ConnectionResetError marks the coalescer closed;
    subsequent writes drop silently (mirrors BaseSender's guard)."""
    w = _RecordingWriter(fail=ConnectionResetError)
    c = ConnCoalescer(w, hold_us=0)
    await c.write(b'A')   # raises inside, swallowed
    await c.write(b'B')   # dropped, no raise
    assert w.writes == []  # nothing recorded (writer raised both times were it reached)


@pytest.mark.asyncio
async def test_flush_before_close_preserves_a_trailing_batch():
    """The teardown flush() path: a batch held when the connection closes is
    still delivered."""
    w = _RecordingWriter()
    c = ConnCoalescer(w, hold_us=50_000)
    await c.write(b'resp1')   # immediate
    await c.write(b'resp2')   # held
    await c.write(b'resp3')   # held
    # Connection tears down before the timer fires:
    await c.flush()
    assert w.writes == [b'resp1', b'resp2resp3']
