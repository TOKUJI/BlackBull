"""The receive decisions belong to ``BufferReader``, not to the transport front end.

``BufferReader`` is the only object that knows both the *demand* (``read(n)``,
``read_head``) and the *consumption* (``take``).  Every decision that follows
from those two — how much to let the transport hand us, when to stop reading,
when a grown allocation goes back to the floor — is therefore the Reader's, and
``ConnectionProtocol`` executes them on the transport without re-deriving them.

Before the split the stop-reading decision lived in
``ConnectionProtocol.buffer_updated`` and reconstructed the Reader's state from
outside: resident bytes, plus "is somebody parked" inferred from the rendezvous
future.  The inference had a gap — the future is cleared when the reader is
*woken*, not when it stops waiting — so an arrival landing in that window armed
a pause that the very next park released.  These tests assert the decisions
themselves, so the inference cannot come back.

The ownership after the split:

* ``BufferReader`` — the receive decisions **and** the receive-side memory
  policy (backpressure watermarks, peak tracking, release hysteresis).
* ``ConnectionProtocol`` — the transport callbacks and executing
  ``pause_reading`` / ``resume_reading``; the rendezvous future stays here
  because it is a callback↔coroutine handoff, not a receive decision.
* ``ReadBuffer`` — bytes, scanning, and growth; it reports a drained boundary
  but never decides what to do about one.

Unit tests, no I/O: the protocol is fed the way a real transport feeds it.
"""
import asyncio

import pytest

from blackbull.server.connection_protocol import (
    _HIGH_WATER,
    _LOW_WATER,
    _RELEASE_HYSTERESIS,
    ConnectionProtocol,
)
from blackbull.server.read_buffer import ReadBuffer

pytestmark = pytest.mark.asyncio

#: A message whose body grows the buffer past the floor but stays under the
#: high-water mark, so it never trips backpressure on the way in.
_LARGE = 100_000


class _FakeTransport:
    """Counts pause/resume as well as recording state — the churn assertions
    below are about how *often* the decision fires, not only its outcome."""

    def __init__(self):
        self.paused = False
        self.pauses = 0
        self.resumes = 0
        self.closed = False

    def pause_reading(self):
        self.paused = True
        self.pauses += 1

    def resume_reading(self):
        self.paused = False
        self.resumes += 1

    def write(self, data): pass
    def writelines(self, parts): pass
    def close(self): self.closed = True
    def is_closing(self): return self.closed
    def get_extra_info(self, name, default=None): return default


@pytest.fixture
def wired():
    proto = ConnectionProtocol()
    transport = _FakeTransport()
    proto.connection_made(transport)
    return proto, transport


def _deliver(proto: ConnectionProtocol, data: bytes) -> int:
    """Feed *data* as the selector transport does — in windows, honouring the
    pause.  Returns how much was actually delivered."""
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


async def _small_message(proto) -> None:
    """One fully-consumed small request: the shape the release hysteresis counts."""
    _deliver(proto, b'GET / HTTP/1.1\r\n\r\n')
    await proto.reader.read_head(limit=8192)


# ---------------------------------------------------------------------------
# The stop-reading decision
# ---------------------------------------------------------------------------

class TestTheStopReadingDecision:
    async def test_backpressure_arms_when_no_reader_is_waiting(self, wired):
        """The decision itself, unchanged by the move: a handler that is
        *behind* must not let a fast peer grow the buffer without bound."""
        proto, transport = wired
        sent = _deliver(proto, b'z' * (1024 * 1024))
        assert transport.paused, 'transport was not paused past the watermark'
        assert sent < 1024 * 1024, 'delivery was never throttled'
        assert proto.reader.buffered_len() >= _HIGH_WATER

    async def test_a_waiting_reader_arms_no_backpressure_at_all(self, wired):
        """A reader parked in ``readexactly`` is starved, not behind — so no
        arrival while it waits may arm a pause.

        The old inference allowed a handful through: the rendezvous future is
        cleared when the reader is *woken*, so an arrival between the wake and
        the reader actually running looked like "nobody is waiting".  The
        Reader knows directly, so the count here is exactly zero rather than
        "a handful is fine".
        """
        proto, transport = wired
        want = _HIGH_WATER * 8

        task = asyncio.create_task(proto.reader.readexactly(want))
        await asyncio.sleep(0)                     # let the reader park

        arrivals = 0
        while arrivals * 8192 < want:
            view = proto.get_buffer(-1)
            n = min(len(view), 8192)
            view[:n] = b'z' * n
            proto.buffer_updated(n)
            arrivals += 1
            await asyncio.sleep(0)                 # give the reader its turn

        assert len(await asyncio.wait_for(task, timeout=5)) == want
        assert arrivals >= 64, 'harness must deliver many arrivals to be meaningful'
        assert transport.pauses == 0, (
            f'{transport.pauses} pauses over {arrivals} arrivals while a reader '
            f'was parked — the stop-reading decision is inferring the reader\'s '
            f'state again instead of reading it')

    async def test_parking_releases_an_armed_pause(self, wired):
        """The release is the Reader's decision too: the bytes it is about to
        wait for are precisely the ones the pause is refusing to read.

        Driven through the reader, because that is where the decision now
        lives — the protocol's rendezvous is only a park.
        """
        proto, transport = wired
        _deliver(proto, b'z' * (1024 * 1024))
        assert transport.paused

        task = asyncio.create_task(proto.reader.readexactly(_HIGH_WATER * 4))
        await asyncio.sleep(0)
        assert not transport.paused, (
            'the reader parked without releasing the pause — the bytes it '
            'waits for can never arrive')

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    async def test_draining_below_the_low_mark_resumes(self, wired):
        proto, transport = wired
        sent = _deliver(proto, b'z' * (1024 * 1024))
        assert transport.paused
        await proto.reader.readexactly(sent - _LOW_WATER)
        assert not transport.paused, 'transport stayed paused after draining'

    async def test_the_protocol_routes_the_decision_through_the_reader(self, wired):
        """``buffer_updated`` executes; it does not judge.

        With a Reader that never asks for a pause, a delivery far past the
        watermark must not pause anything — the front end has no watermark of
        its own to compare against.  This is the assertion that fails if the
        stop-reading test creeps back into the transport callback.
        """
        proto, transport = wired

        class _StubReader:
            def __init__(self):
                self.arrivals = 0

            def data_arrived(self):
                self.arrivals += 1

        proto.reader = _StubReader()
        _deliver(proto, b'z' * (_HIGH_WATER * 2))

        assert proto.reader.arrivals > 0, 'the Reader was never told bytes landed'
        assert transport.pauses == 0, (
            'the transport front end paused without a Reader asking it to')


# ---------------------------------------------------------------------------
# The receive-side memory policy
# ---------------------------------------------------------------------------

class TestTheMemoryPolicy:
    async def test_a_grown_allocation_survives_its_own_message(self, wired):
        """Hysteresis, half one: the message that grew the buffer does not
        immediately give it back, or a connection repeating large messages
        churns grow+shrink per message (the F6/B7 finding)."""
        proto, _ = wired
        _deliver(proto, b'H\r\n\r\n')
        await proto.reader.read_head(limit=8192)
        _deliver(proto, b'z' * _LARGE)
        await proto.reader.readexactly(_LARGE)

        assert proto.buffer.capacity > _LARGE // 2, 'buffer never grew'
        assert proto.buffer.capacity > ReadBuffer.FLOOR, (
            'the grown allocation was handed back at its own message boundary')

    async def test_a_grown_allocation_returns_to_the_floor_after_small_messages(
            self, wired):
        """Hysteresis, half two: a connection that has gone back to small
        messages must not hold its peak for the rest of its keep-alive life.

        Boundaries are forced here rather than waited for: the buffer compacts
        on its own schedule (consumed prefix ≥ half the allocation), so a
        natural run would need thousands of small requests per boundary and
        would assert the compaction threshold instead of the policy.
        """
        proto, _ = wired
        _deliver(proto, b'H\r\n\r\n')
        await proto.reader.read_head(limit=8192)
        _deliver(proto, b'z' * _LARGE)
        await proto.reader.readexactly(_LARGE)
        grown = proto.buffer.capacity
        assert grown > ReadBuffer.FLOOR

        for i in range(_RELEASE_HYSTERESIS - 1):
            proto.buffer.compact()                 # the message boundary
            await _small_message(proto)            # evaluates the pending one
            assert proto.buffer.capacity == grown, (
                f'released after {i + 1} small messages — the hysteresis is '
                f'{_RELEASE_HYSTERESIS}')

        proto.buffer.compact()
        await _small_message(proto)
        assert proto.buffer.capacity == ReadBuffer.FLOOR, (
            f'stayed at {proto.buffer.capacity} after '
            f'{_RELEASE_HYSTERESIS} small messages')

    async def test_repeated_large_messages_reuse_the_grown_allocation(self, wired):
        """Each large message re-arms the counter, so the allocation survives
        a connection that keeps serving them."""
        proto, _ = wired
        _deliver(proto, b'H\r\n\r\n')
        await proto.reader.read_head(limit=8192)
        _deliver(proto, b'z' * _LARGE)
        await proto.reader.readexactly(_LARGE)
        grown = proto.buffer.capacity

        for _ in range(_RELEASE_HYSTERESIS + 2):
            # No forced boundary: consuming a message this size compacts on its
            # own, which is the boundary the policy hangs off.
            _deliver(proto, b'H\r\n\r\n')
            await proto.reader.read_head(limit=8192)
            _deliver(proto, b'z' * _LARGE)
            await proto.reader.readexactly(_LARGE)
            assert proto.buffer.capacity == grown, (
                f'capacity changed {grown} -> {proto.buffer.capacity} between '
                f'large messages')
