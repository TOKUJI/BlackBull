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
  policy (backpressure watermarks, peak tracking, release hysteresis), plus
  every consuming read, ``discard`` included: throwing bytes away is reading
  them.
* ``ConnectionProtocol`` — the transport callbacks and executing
  ``pause_reading`` / ``resume_reading``; the rendezvous future stays here
  because it is a callback↔coroutine handoff, not a receive decision.
* ``ReadBuffer`` — bytes, scanning, and growth; it reports a drained boundary
  but never decides what to do about one, and it closes that boundary out
  itself when the reader says the message is done.

The last section asserts the rule that makes publishing survivable: a
published field has exactly one writer.  Reading across the boundary is free
and is what keeps the arrival path free of a call; *writing* across it moves a
state transition without moving the ownership, and then only a comment says
who is responsible.

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

    # The lingering close writes FIN before draining.  Modelled because a fake
    # that lacks it raises inside ``linger_close``'s own ``except Exception``,
    # which silently skipped the drain the tests below are about.
    def can_write_eof(self): return True

    def write_eof(self): self.eof_written = True


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
        """The transport front end compares the byte threshold; it never pauses.

        With a Reader that never asks for a pause, a delivery far past the
        watermark must not pause anything — the front end executes, and the
        Reader decides.  This is the assertion that fails if the pause
        decision creeps back into the transport callback.
        """
        proto, transport = wired

        class _StubReader:
            def __init__(self):
                self.crossings = 0
                self.read_offer = 0

            def maybe_pause(self):
                self.crossings += 1

        proto.reader = _StubReader()
        _deliver(proto, b'z' * (_HIGH_WATER * 2))

        assert proto.reader.crossings > 0, (
            'the Reader was never consulted on a high-water crossing')
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

        assert proto._rb.capacity > _LARGE // 2, 'buffer never grew'
        assert proto._rb.capacity > ReadBuffer.FLOOR, (
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
        grown = proto._rb.capacity
        assert grown > ReadBuffer.FLOOR

        for i in range(_RELEASE_HYSTERESIS - 1):
            proto._rb.compact()                 # the message boundary
            await _small_message(proto)            # evaluates the pending one
            assert proto._rb.capacity == grown, (
                f'released after {i + 1} small messages — the hysteresis is '
                f'{_RELEASE_HYSTERESIS}')

        proto._rb.compact()
        await _small_message(proto)
        assert proto._rb.capacity == ReadBuffer.FLOOR, (
            f'stayed at {proto._rb.capacity} after '
            f'{_RELEASE_HYSTERESIS} small messages')

    async def test_repeated_large_messages_reuse_the_grown_allocation(self, wired):
        """Each large message re-arms the counter, so the allocation survives
        a connection that keeps serving them."""
        proto, _ = wired
        _deliver(proto, b'H\r\n\r\n')
        await proto.reader.read_head(limit=8192)
        _deliver(proto, b'z' * _LARGE)
        await proto.reader.readexactly(_LARGE)
        grown = proto._rb.capacity

        for _ in range(_RELEASE_HYSTERESIS + 2):
            # No forced boundary: consuming a message this size compacts on its
            # own, which is the boundary the policy hangs off.
            _deliver(proto, b'H\r\n\r\n')
            await proto.reader.read_head(limit=8192)
            _deliver(proto, b'z' * _LARGE)
            await proto.reader.readexactly(_LARGE)
            assert proto._rb.capacity == grown, (
                f'capacity changed {grown} -> {proto._rb.capacity} between '
                f'large messages')


# ---------------------------------------------------------------------------
# The transport offer follows the read demand
# ---------------------------------------------------------------------------

class TestTheTransportOfferFollowsTheDemand:
    """The arrival granularity is the reader's call: a parked read offers the
    transport a recv window of up to ``min(demand, high-water)``, so one
    arrival feeds most of the read instead of a floor-sized dribble.  This is
    what keeps a transport-paced (up-to-n) body read from collapsing to the
    8 KiB delivery-pinning that sank the first attempt.

    The decision is the reader's; the *delivery* of it is a published
    attribute, the mirror of ``reading_paused`` going the other way.  Which
    side stores it is not a detail here — the arrival path touches it on
    every connection, so a poll in that position is a per-arrival cost."""

    async def test_idle_reader_offers_nothing(self, wired):
        proto, _ = wired
        assert proto.reader.read_offer == 0

    async def test_parked_read_declares_its_demand(self, wired):
        proto, _ = wired
        task = asyncio.create_task(proto.reader.readexactly(64 * 1024))
        await asyncio.sleep(0)
        assert proto.reader.read_offer == 64 * 1024
        _deliver(proto, b'x' * (64 * 1024))
        await task
        assert proto.reader.read_offer == 0

    async def test_demand_is_capped_at_the_high_water_mark(self, wired):
        proto, _ = wired
        task = asyncio.create_task(proto.reader.read(1 << 30))
        await asyncio.sleep(0)
        assert proto.reader.read_offer == _HIGH_WATER
        _deliver(proto, b'y')
        assert await task == b'y'
        assert proto.reader.read_offer == 0

    async def test_one_recv_can_feed_the_whole_demand(self, wired):
        """The point of the offer: one get_buffer + buffer_updated pair
        delivers the entire parked read, not an 8 KiB slice of it."""
        proto, _ = wired
        task = asyncio.create_task(proto.reader.readexactly(64 * 1024))
        await asyncio.sleep(0)
        view = proto.get_buffer(-1)
        assert len(view) >= 64 * 1024, 'the offer did not follow the demand'
        view[:64 * 1024] = b'z' * (64 * 1024)
        proto.buffer_updated(64 * 1024)
        assert await task == b'z' * (64 * 1024)
        assert proto.reader.read_offer == 0

    async def test_idle_get_buffer_stays_at_the_floor(self, wired):
        """No pending read → the idle offer is the floor span, so the
        idle-memory floor is untouched (the F5 finding holds)."""
        proto, _ = wired
        view = proto.get_buffer(-1)
        try:
            assert len(view) == ReadBuffer.FLOOR
        finally:
            view.release()

    async def test_the_arrival_path_reads_the_offer_without_calling_the_reader(
            self, wired):
        """Published, not polled — the distinction is *call* versus *read*.

        ``get_buffer`` runs on every arrival on every connection, including
        the ones that never read a body, and the EC2 /conn A/B showed a method
        call in that position at the same order as a whole regression.  So the
        rule is that the arrival path asks the reader nothing: it reads the
        field the reader publishes.  Reaching the owner costs one attribute
        hop (measured at 1.66 ns), which is what keeps the field on the object
        that decides it without reinstating the call.

        The stand-in permits exactly that one read and refuses everything
        else, so a call creeping back fails here.
        """
        proto, _ = wired

        class _FieldOnly:
            read_offer = 0

            def __getattr__(self, name):
                raise AssertionError(
                    f'the arrival path called into the reader ({name}) for '
                    f'the offer; it may only read the published field')

        proto.reader = _FieldOnly()
        view = proto.get_buffer(-1)
        try:
            assert len(view) == ReadBuffer.FLOOR
        finally:
            view.release()


class TestTheOfferIsPublishedOnlyByTheParkingPath:
    """An offer exists to size the recv that will *wake* a parked reader.

    So the reader declares one when it parks, and at no other time.  A read
    the buffer can already satisfy never reaches the transport at all — the
    demand it would publish could not be consulted by anyone, because nothing
    yields between the publish and the return.  Publishing it anyway is pure
    per-read cost, and on HTTP/2 the great majority of reads are that kind.

    These assert the property rather than the saving: the write itself is
    observable, so a future change that re-arms an offer outside the parking
    path fails here instead of quietly reappearing in a benchmark.
    """

    @staticmethod
    def _recording(proto):
        """Record every write to the reader's ``read_offer``, in order."""
        writes: list[int] = []
        reader = proto.reader
        cls = type(reader)
        slot = cls.read_offer          # the slot descriptor the property hides

        class _Recorded(cls):
            __slots__ = ()

            @property
            def read_offer(self):
                return slot.__get__(self, cls)

            @read_offer.setter
            def read_offer(self, value):
                writes.append(value)
                slot.__set__(self, value)

        reader.__class__ = _Recorded
        writes.clear()
        return writes

    async def test_a_read_the_buffer_can_satisfy_publishes_no_offer(self, wired):
        proto, _ = wired
        _deliver(proto, b'abcdefgh')
        writes = self._recording(proto)

        assert await proto.reader.read(4) == b'abcd'

        assert not [w for w in writes if w], (
            f'a read that never parked still declared a demand to the '
            f'transport: {writes!r}.  Nothing can consult it — there is no '
            f'await between the publish and the return — so it is cost with '
            f'no reader.')

    async def test_readexactly_that_is_already_satisfied_publishes_no_offer(
            self, wired):
        proto, _ = wired
        _deliver(proto, b'x' * 64)
        writes = self._recording(proto)

        assert await proto.reader.readexactly(32) == b'x' * 32

        assert not [w for w in writes if w], writes

    async def test_a_parked_read_still_declares_its_demand(self, wired):
        """The half that must not be lost: an offer is exactly what makes one
        arrival feed a whole parked read."""
        proto, _ = wired
        writes = self._recording(proto)

        task = asyncio.create_task(proto.reader.readexactly(64 * 1024))
        await asyncio.sleep(0)
        assert proto.reader.read_offer == 64 * 1024, (
            'a parked read declared no demand — the arrival that wakes it '
            'will be sized at the idle floor')

        _deliver(proto, b'y' * (64 * 1024))
        await task
        assert proto.reader.read_offer == 0
        assert writes and writes[-1] == 0, writes

    async def test_the_head_read_keeps_the_floor_offer(self, wired):
        """Deliberately unchanged.

        ``read_head`` parks like any other read but declares nothing, so a
        header arrival is sized at the floor.  Giving it a demand would change
        the arrival granularity of every request's first read — a separate
        question with its own measurement, not something to acquire as a side
        effect of moving where the offer is published.
        """
        proto, _ = wired
        writes = self._recording(proto)

        task = asyncio.create_task(proto.reader.read_head(limit=8192))
        await asyncio.sleep(0)
        assert proto.reader.read_offer == 0, (
            f'read_head began declaring a demand: {writes!r}.  That changes '
            f'header arrival granularity; measure it as its own change.')

        _deliver(proto, b'GET / HTTP/1.1\r\n\r\n')
        await task

    async def test_the_offer_is_non_zero_only_while_a_reader_waits(self, wired):
        """The invariant the publication site exists to hold.

        ``read_offer`` and the reader's ``_waiting`` flag are two faces of one
        fact — somebody is parked, and this is how much they want — so they
        are maintained together or they drift.
        """
        proto, _ = wired
        reader = proto.reader
        assert proto.reader.read_offer == 0 and not reader._waiting

        task = asyncio.create_task(proto.reader.read(4096))
        await asyncio.sleep(0)
        assert reader._waiting is True and proto.reader.read_offer != 0

        _deliver(proto, b'z' * 4096)
        await task
        assert reader._waiting is False and proto.reader.read_offer == 0


# ---------------------------------------------------------------------------
# Ownership: what each object is allowed to touch
# ---------------------------------------------------------------------------

class TestEachPublishedFieldHasOneWriter:
    """A published field is a *fact*, not shared state.

    Publishing trades encapsulation for speed: the other side reads the
    attribute instead of calling, which is what keeps the arrival path free of
    a method call.  That trade is safe while the field has exactly **one**
    writer — the owner still decides, the other side only looks.  It stops
    being safe the moment the consumer writes back, because then a state
    transition has crossed the boundary without the ownership crossing with
    it, and nothing but a comment says who is responsible for it.

    Asserted on the source rather than at runtime: the rule is "this class
    contains no assignment to that field", which is a property of the code,
    not of any one execution path.
    """

    @staticmethod
    def _assignments_to(cls, names):
        import ast
        import inspect
        import textwrap
        tree = ast.parse(textwrap.dedent(inspect.getsource(cls)))
        found = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                targets = node.targets
            elif isinstance(node, (ast.AugAssign, ast.AnnAssign)):
                targets = [node.target]
            else:
                continue
            for t in targets:
                if isinstance(t, ast.Attribute) and t.attr in names:
                    found.append(f'{t.attr} at line {t.lineno} of the class')
        return found

    def test_the_reader_never_writes_the_buffers_fields(self):
        """``drained_boundary`` and ``peak_avail`` are the buffer's.

        The buffer raises the boundary and accumulates the peak; the reader
        *reads* both to decide whether to hand an allocation back.  Clearing
        them is the producer's own transition — a consumer that resets them is
        an edge-triggered signal with the reset on the wrong side, which loses
        an edge the moment a second consumer exists.
        """
        from blackbull.server.connection_protocol import BufferReader
        bad = self._assignments_to(
            BufferReader, {'drained_boundary', 'peak_avail'})
        assert not bad, (
            f'BufferReader assigns to the buffer\'s own fields: {bad}.  '
            f'Read them to decide; ask the buffer to clear them.')

    def test_the_buffer_never_writes_the_readers_fields(self):
        """And the rule holds in the other direction."""
        from blackbull.server.read_buffer import ReadBuffer
        bad = self._assignments_to(ReadBuffer, {'read_offer', '_waiting'})
        assert not bad, bad

    def test_the_protocol_never_writes_the_offer_it_only_reads(self):
        """``read_offer`` is the reader's demand; the protocol consults it."""
        from blackbull.server.connection_protocol import ConnectionProtocol
        bad = self._assignments_to(ConnectionProtocol, {'read_offer'})
        assert not bad, (
            f'ConnectionProtocol assigns to read_offer: {bad}.  The demand is '
            f'the reader\'s; the protocol only reads it on the arrival path.')


class TestTheProtocolOwnsNoReceiveWork:
    """What is left on the transport front end must be socket work only."""

    async def test_the_offer_lives_on_the_reader_that_decides_it(self, wired):
        proto, _ = wired
        assert hasattr(proto.reader, 'read_offer'), (
            'the recv demand does not live on the object that decides it')

        task = asyncio.create_task(proto.reader.readexactly(64 * 1024))
        await asyncio.sleep(0)
        assert proto.reader.read_offer == 64 * 1024
        _deliver(proto, b'z' * (64 * 1024))
        await task
        assert proto.reader.read_offer == 0

    async def test_the_protocol_hands_out_no_raw_buffer(self, wired):
        """The reader is the only object that talks to the buffer.

        A public accessor for the whole buffer is an official way round that,
        and this one had no caller outside the tests: its stated purpose — a
        successor taking the stream over — is served by handing over the
        *reader*, which is what the h2c and WebSocket upgrades actually do.
        """
        proto, _ = wired
        assert not hasattr(proto, 'buffer'), (
            'ConnectionProtocol still exposes its ReadBuffer wholesale')

    async def test_the_two_eof_questions_have_two_names(self, wired):
        """"the peer closed its end" and "there is nothing left to read" are
        different questions, and one name for both makes every call site a
        guess about which was meant."""
        proto, _ = wired
        _deliver(proto, b'tail')
        proto.eof_received()

        assert proto.peer_closed is True, 'the transport fact is not readable'
        assert proto.reader.at_eof() is False, (
            'the reader reported EOF while it still holds bytes to serve')
        assert await proto.reader.read(4) == b'tail'
        assert proto.reader.at_eof() is True

    async def test_discarding_is_a_read_and_takes_a_reads_decisions(self, wired):
        """The lingering close throws bytes away, which is still reading.

        So it belongs to the reader and makes the decisions every other read
        makes — here, releasing a pause the discarded bytes had armed.  Doing
        it on the protocol with a direct ``consume`` skipped all of them.
        """
        proto, transport = wired
        _deliver(proto, b'z' * (1024 * 1024))
        assert transport.paused, 'the delivery did not arm backpressure'

        loop = asyncio.get_running_loop()
        await proto.reader.discard(max_bytes=1024 * 1024,
                                   deadline=loop.time() + 0.05)

        assert not transport.paused, (
            'discarding drained the buffer without releasing the pause — the '
            'discard bypassed the reader\'s consuming decisions')

    async def test_the_protocol_does_not_read_on_its_own(self, wired):
        """``linger_close`` delegates the draining and keeps the socket half.

        A Reader that refuses to discard proves the protocol has no second,
        private read path: if the bytes still go away, the protocol read them
        itself.
        """
        proto, transport = wired
        _deliver(proto, b'z' * 4096)
        resident = proto.reader.buffered_len()

        class _RefusingReader:
            def __init__(self, inner):
                self._inner = inner
                self.asked = 0

            def __getattr__(self, name):
                return getattr(self._inner, name)

            async def discard(self, max_bytes, deadline):
                self.asked += 1

        proto.reader = _RefusingReader(proto.reader)
        await proto.linger_close()

        assert proto.reader.asked == 1, (
            'linger_close did not ask the reader to discard')
        assert proto.reader.buffered_len() == resident, (
            'the bytes were consumed anyway — the protocol still has its own '
            'read path into the buffer')
        assert transport.closed, 'linger_close must still close the socket'
