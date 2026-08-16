"""What must be true of a connection's receive state *between* requests.

These are the invariants the memory-leak investigation checked with a
throwaway 1000-iteration harness (proposal
``receive-path-memory-leak-investigation.md``, closed 2026-08-15 — the leak
was in seven test doubles, and the production path was flat).  A one-off
script proves a thing once; the suite proves it on every change, which is the
point of moving them here.

Each invariant answers "what would a real leak look like from inside the
process?", and each is a property of the *idle* connection — the state left
behind after a request completes, however it completed:

* the published read offer is back to zero, so ``get_buffer`` offers the idle
  floor span and not the last read's demand;
* the buffer's capacity is back at its floor once the release hysteresis has
  seen enough small messages, so a connection that once handled a big body
  does not hold that allocation for its lifetime;
* no ``memoryview`` export outlives the arrival that needed it, because a
  live export pins the buffer and blocks every resize.

The exit paths matter more than the happy one: a disconnect, a timeout, and a
cancellation mid-park are exactly where a "clear it afterwards" line gets
skipped.
"""
import asyncio
import gc

import pytest

from blackbull.server.connection_protocol import (
    _HIGH_WATER, _RELEASE_HYSTERESIS, ConnectionProtocol,
)
from blackbull.server.read_buffer import ReadBuffer

pytestmark = pytest.mark.asyncio


class _FakeTransport:
    def __init__(self):
        self.paused = False

    def pause_reading(self):
        self.paused = True

    def resume_reading(self):
        self.paused = False

    def write(self, data): pass
    def writelines(self, parts): pass
    def close(self): pass
    def is_closing(self): return False
    def get_extra_info(self, name, default=None): return default


@pytest.fixture
def wired():
    proto = ConnectionProtocol()
    proto.connection_made(_FakeTransport())
    return proto


def _deliver(proto: ConnectionProtocol, data: bytes) -> int:
    sent = 0
    while sent < len(data):
        if proto.transport is not None and proto.transport.paused:
            break
        view = proto.get_buffer(-1)
        n = min(len(view), len(data) - sent)
        view[:n] = data[sent:sent + n]
        proto.buffer_updated(n)
        sent += n
    return sent


async def _one_request(proto: ConnectionProtocol, body: bytes = b'') -> bytes:
    """A whole request the way the actor reads one: head, then body."""
    head = (b'POST / HTTP/1.1\r\nHost: x\r\nContent-Length: %d\r\n\r\n'
            % len(body))
    _deliver(proto, head)
    await proto.reader.read_head(limit=8192)
    got = bytearray()
    while len(got) < len(body):
        _deliver(proto, body[len(got):])
        got += await proto.reader.read(len(body) - len(got))
    return bytes(got)


class TestTheReadOfferIsClearedOnEveryExitPath:
    """A demand left behind is a connection that keeps offering the transport a
    large recv window while idle — the memory floor silently gone."""

    async def test_after_a_completed_read(self, wired):
        proto = wired
        await _one_request(proto, b'x' * 4096)
        assert proto.read_offer == 0

    async def test_after_an_eof_mid_read(self, wired):
        proto = wired
        task = asyncio.create_task(proto.reader.readexactly(4096))
        await asyncio.sleep(0)
        proto.eof_received()
        with pytest.raises(Exception):
            await task
        assert proto.read_offer == 0

    async def test_after_a_cancellation_while_parked(self, wired):
        """The path a body timeout takes: the actor's task is cancelled while
        the reader is parked, so the ``finally`` is the only thing that runs."""
        proto = wired
        task = asyncio.create_task(proto.reader.read(_HIGH_WATER))
        await asyncio.sleep(0)
        assert proto.read_offer == _HIGH_WATER, 'the read never parked'
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert proto.read_offer == 0

    async def test_the_idle_offer_is_the_floor_span(self, wired):
        """The consequence the invariant exists for."""
        proto = wired
        await _one_request(proto, b'x' * 4096)
        view = proto.get_buffer(-1)
        try:
            assert len(view) == ReadBuffer.FLOOR
        finally:
            view.release()


class TestTheBufferIsBoundedAndGivenBack:
    """Two different promises, and only one of them is prompt.

    A grown buffer is *bounded* immediately — nothing after the big message
    grows it further, so the connection's cost is its own peak and not a
    ratchet.  Handing that peak back is deferred: the release is only taken at
    a drained compaction boundary, and a stream of small messages does not
    compact until the write cursor reaches the end of the allocation.  The
    price is one walk of the buffer; the guarantee is that it is bounded the
    whole way.
    """

    async def test_capacity_is_bounded_by_the_peak_a_message_needed(self, wired):
        """The shape a slow leak would take: capacity that climbs per request
        and never comes down."""
        proto = wired
        await _one_request(proto, b'x' * 200_000)
        peak = len(proto.buffer._buf)
        for _ in range(200):
            await _one_request(proto, b'y' * 16)
        assert len(proto.buffer._buf) == peak, 'capacity ratcheted upward'
        assert proto.buffer.available == 0

    async def test_the_peak_is_handed_back_once_the_allocation_is_walked(
            self, wired):
        """It does come back — the deferral is not a leak with better manners.

        The count is a walk of the allocation, not ``_RELEASE_HYSTERESIS``:
        small messages are consumed without compacting, so the boundary the
        release waits for only arrives when the write cursor runs out of room.
        The bound below is that walk with slack, so the test fails if the
        release stops happening at all — which is the regression worth catching.
        """
        proto = wired
        await _one_request(proto, b'x' * 200_000)
        assert proto.buffer.grown, 'the large body never grew the buffer'
        bound = len(proto.buffer._buf)      # far more messages than a walk needs
        for i in range(bound):
            await _one_request(proto, b'y' * 16)
            if not proto.buffer.grown:
                break
        else:
            pytest.fail(f'the peak was never released in {bound} small messages')
        assert i >= _RELEASE_HYSTERESIS, (
            'released before the hysteresis had seen its small messages')

    async def test_a_connection_that_stays_small_never_grows(self, wired):
        proto = wired
        for _ in range(50):
            await _one_request(proto, b'z' * 1024)
        assert not proto.buffer.grown
        assert proto.buffer.available == 0


class TestNoExportOutlivesItsArrival:
    async def test_the_view_is_dropped_after_every_request(self, wired):
        """A live ``memoryview`` export pins the bytearray: every later resize
        raises ``BufferError``, so a retained view is not a slow leak but an
        immediate deadlock waiting to happen."""
        proto = wired
        for _ in range(5):
            await _one_request(proto, b'q' * 2048)
            assert proto.buffer._view is None


class TestNothingIsRetainedAcrossConnections:
    async def test_finished_connections_are_collectable(self):
        """The direct question a leak asks.  ``ReadBuffer`` uses ``__slots__``
        without ``__weakref__``, so the live count is taken from the GC rather
        than from a weak reference — the same way the investigation counted it.
        """
        def _live() -> int:
            return sum(1 for o in gc.get_objects()
                       if type(o).__name__ == 'ReadBuffer')

        gc.collect()
        before = _live()
        for _ in range(20):
            proto = ConnectionProtocol()
            proto.connection_made(_FakeTransport())
            await _one_request(proto, b'w' * 4096)
            proto.connection_lost(None)
            del proto
        gc.collect()
        assert _live() <= before, (
            'connection buffers outlived their connections')
