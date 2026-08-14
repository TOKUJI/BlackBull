"""Connection transport front end: `asyncio.BufferedProtocol` over one buffer.

One of these per accepted connection, created before the protocol is known —
the shared listener detects HTTP/1.1, h2c, and MQTT off the same resident
bytes, so the buffer belongs to the *connection*, not to any one protocol.

This is what replaces `asyncio.StreamReader` on the inbound path.  The
kernel writes straight into the connection's :class:`~.read_buffer.ReadBuffer`
through :meth:`ConnectionProtocol.get_buffer`, and the actor's coroutine parks on a
future that :meth:`ConnectionProtocol.buffer_updated` resolves.

**The actor invariant is intact.**  One coroutine still owns the connection's
state and processes one request at a time; it is woken by the protocol instead
of parking inside `readuntil`.  That distinction is the whole design: sanic
reaches half our read/parse cost with a parked coroutine too, so the cost was
never the coroutine — it was reading through a second buffer.

:class:`BufferReader` presents the :class:`~.recipient.AbstractReader` surface
so the body recipient and the WebSocket/h2c successors work unchanged, and adds
:meth:`BufferReader.read_head` — the one-scan header read the H/1.1 actor uses
instead of a `readuntil` per line.

**The two classes split decision from execution.**  The reader owns the receive
competence — whether a high-water crossing should pause, when to wait, and
when a hand a grown buffer back — because reading is the only activity that
knows both what was asked for and what was consumed.  The protocol owns the
socket: the transport callbacks, the rendezvous future, the two flow-control
calls it makes when the reader asks, and the byte-level high-water comparison
(a threshold, not a judgement — the reader decides what a crossing means).
Before the split the stop-reading test sat in
:meth:`ConnectionProtocol.buffer_updated` and reconstructed the reader's
state from outside — resident bytes plus a guess at whether anyone was parked.

Because peeked bytes stay resident, protocol detection can decide without
consuming: there is nothing to replay to the winning binding, which is what
retires `PrefixReader` on this path.
"""
from __future__ import annotations

import asyncio

from .read_buffer import ReadBuffer
from .recipient import AbstractReader, IncompleteReadError, ReadLimitExceeded

__all__ = ('BufferReader', 'ConnectionProtocol')

#: Stop reading from the transport once this many unconsumed bytes are resident.
#: Backpressure's memory half: without it a fast peer feeding a slow handler
#: grows the buffer without bound.  Matched to the write-side watermark shape.
_HIGH_WATER = 128 * 1024

#: Resume once the buffer falls back to this.  A gap between the two stops the
#: transport being paused and resumed on alternate reads.
_LOW_WATER = 32 * 1024

#: Fully-consumed *small* messages a grown buffer must survive before
#: :meth:`BufferReader._at_boundary` returns it to the floor.  Hysteresis: a
#: keep-alive connection that repeats a large message reuses its grown
#: allocation instead of churning grow+shrink per message (F6 follow-up); the
#: peak is given back once the connection has shown a few messages that did
#: not need it.
_RELEASE_HYSTERESIS = 4


class BufferReader(AbstractReader):
    """`AbstractReader` over a :class:`ReadBuffer` fed by :class:`ConnectionProtocol`.

    Every method serves from resident bytes first and only parks when it needs
    more.  A pipelined or keep-alive peer's next head is usually already
    resident, so those reads complete without a loop turn — which is the claim
    the layered predecessor made and could not deliver, because it sat on a
    reader that was buffering underneath it.

    **This is where the receive decisions live.**  Reading is the only activity
    that knows both the demand (``read(n)``, :meth:`read_head`) and the
    consumption (``take``), so everything that follows from the pair is
    decided here and merely *executed* on the transport: whether a high-water
    crossing pauses the peer (:meth:`maybe_pause`), when to let it go again
    (:meth:`_consumed`), and when a grown allocation goes back to the floor
    (:meth:`_at_boundary`).  The protocol below owns the socket, not the
    judgement about it.

    Deliberately not on :class:`~.recipient.AbstractReader`: two of its three
    implementations have no transport to pause, so promoting this competence to
    the interface would force a no-op onto them.
    """

    __slots__ = ('_buf', '_proto', '_release_count', '_waiting')

    def __init__(self, buf: ReadBuffer, proto: ConnectionProtocol) -> None:
        self._buf = buf
        self._proto = proto
        #: This reader is parked waiting for bytes.  Held here rather than
        #: inferred from the protocol's rendezvous future: the future is
        #: cleared when the reader is *woken*, so an arrival landing between
        #: the wake and the reader running read as "nobody is waiting" and
        #: armed a pause that the next park immediately released.
        self._waiting = False
        self._release_count = 0  # consecutive small messages since the last grow

    # -- the receive decisions ---------------------------------------------

    def maybe_pause(self) -> None:
        """A delivery crossed the high-water mark: decide whether to pause.

        Called by the protocol only when the resident count crossed the mark —
        the byte threshold is compared at the transport, which already holds
        the count and accounts it, so this reader call is the rare crossing
        rather than a per-arrival one.

        Not while this reader is waiting: it is starved, not behind, so the
        condition backpressure exists to prevent is not the one in play.
        Pausing anyway would cost a ``pause_reading``/``resume_reading`` pair —
        two ``epoll_ctl`` calls — on every arrival for the whole of a large
        read, since the next park releases it again.
        """
        if not self._waiting:
            self._proto.pause_reading()

    def _consumed(self) -> None:
        """A read took bytes out: the two decisions that follow from that.

        Releasing backpressure is gated on the low mark rather than on any
        consumption, so the gap between the marks stops the transport being
        paused and resumed on alternate reads.

        The message boundary is answered here and nowhere else, late rather
        than eagerly.  ``compact()`` also raises it from the *arrival* path
        (``_make_room`` compacts before growing), and a delivery lands
        immediately after that — so answering it there could never release
        anything, and would consume the boundary that this path can act on.
        """
        proto, buf = self._proto, self._buf
        if proto.reading_paused and buf.available <= _LOW_WATER:
            proto.resume_reading()
        if buf.drained_boundary:
            buf.drained_boundary = False
            self._at_boundary()

    def _at_boundary(self) -> None:
        """A message is provably gone: decide whether to keep its allocation.

        Hysteretic.  A message whose peak exceeded the floor re-arms the
        counter, so a connection that keeps serving large messages reuses its
        allocation instead of growing and shrinking per message (the F6/B7
        churn); only after ``_RELEASE_HYSTERESIS`` fully-consumed *small*
        messages is the peak given back.
        """
        buf = self._buf
        if buf.grown:
            if buf.peak_avail > buf.FLOOR:
                self._release_count = 0
            else:
                self._release_count += 1
                if (self._release_count >= _RELEASE_HYSTERESIS
                        and buf.release_to_floor()):
                    self._release_count = 0
        buf.peak_avail = 0

    async def wait_for_data(self) -> None:
        """Park until more arrives, declaring the wait first.

        Parking releases the high-water pause.  Backpressure exists to stop a
        fast peer outrunning a handler that is *behind*; a reader about to
        block is the opposite case — it is starved, and the bytes it waits for
        are precisely the ones the pause is refusing to read.  Without this,
        any single read larger than the mark deadlocks: a WebSocket frame, or
        a ``chunked`` chunk whose size the peer chose.
        ``asyncio.StreamReader._wait_for_data`` resumes here for the same
        reason.
        """
        proto = self._proto
        if proto.reading_paused:
            proto.resume_reading()
        self._waiting = True
        try:
            await proto.wait_for_arrival()
        finally:
            self._waiting = False

    # -- AbstractReader ----------------------------------------------------

    async def read(self, n: int = -1) -> bytes:
        if not self._buf.available:
            if self._proto.at_eof:
                return b''
            await self.wait_for_data()
            if not self._buf.available:
                return b''
        take = self._buf.available if n < 0 else min(n, self._buf.available)
        out = self._buf.take(take)
        self._consumed()
        return out

    async def readexactly(self, n: int) -> bytes:
        while self._buf.available < n:
            if self._proto.at_eof:
                partial = self._buf.take(self._buf.available)
                raise IncompleteReadError(partial)
            await self.wait_for_data()
        out = self._buf.take(n)
        self._consumed()
        return out

    async def readuntil(self, sep: bytes = b'\n') -> bytes:
        while True:
            idx = self._buf.find(sep)
            if idx >= 0:
                out = self._buf.take(idx + len(sep))
                self._consumed()
                return out
            if self._proto.at_eof:
                raise IncompleteReadError(self._buf.take(self._buf.available))
            await self.wait_for_data()

    def has_buffered(self) -> bool:
        return self._buf.available > 0

    def buffered_len(self) -> int:
        return self._buf.available

    def peek(self, n: int | None = None) -> bytes:
        avail = self._buf.available
        want = avail if n is None else min(n, avail)
        return bytes(self._buf.view(want))

    def at_eof(self) -> bool:
        return self._proto.at_eof and not self._buf.available

    async def fill(self, n: int) -> bool:
        """Wait until *n* bytes are resident, consuming nothing.

        Free here: resident bytes are already the buffer's normal state, so
        peeking is just not calling ``take``.  It is the reason detection can
        hand the winning binding this very reader with the stream still whole.
        """
        while self._buf.available < n:
            if self._proto.at_eof:
                return False
            await self.wait_for_data()
        return True

    # -- the one-scan header read ------------------------------------------

    async def read_head(self, limit: int) -> bytes:
        """The message head, terminator included — found in one scan.

        The override the whole rewrite exists for: the terminator is looked for
        once, across everything resident, and the head leaves the buffer in a
        single copy.  Resumable, so bytes already scanned are not scanned again
        when the head arrives split across reads.

        Contract as documented on :meth:`AbstractReader.read_head` — an idle
        close returns ``b''`` and a truncated one raises with the partial.
        """
        while True:
            if not self._buf.available:
                # Nothing resident: go straight to the wait — the "check" of
                # the check-then-wait-then-check pattern.  The empty scan it
                # skips is a control-flow artifact (~0.79 µs/req on EC2, F5):
                # an empty buffer can never be LIMIT_EXCEEDED (0 > limit is
                # false) and the scan's resumption state is untouched (it
                # sets ``_scanned = _w``, which already equals ``_r``), so the
                # idle-close and truncated-head contracts below are unchanged.
                if self._proto.at_eof:
                    return b''
                await self.wait_for_data()
                continue
            end = self._buf.find_head_end(limit=limit)
            if end == ReadBuffer.LIMIT_EXCEEDED:
                # The bytes stay resident: the caller classifies them, and the
                # lingering close still has something to discard.
                raise ReadLimitExceeded(
                    f'head exceeds {limit} bytes',
                    bytes(self._buf.view(min(self._buf.available, limit + 2))))
            if end >= 0:
                out = self._buf.take(end)
                self._consumed()
                return out
            if self._proto.at_eof:
                partial = self._buf.take(self._buf.available)
                if not partial:
                    return b''
                raise IncompleteReadError(partial)
            await self.wait_for_data()


class ConnectionProtocol(asyncio.BufferedProtocol):
    """Buffered-protocol front end for one H/1.1 connection.

    The transport half of the receive path: it owns the socket callbacks, the
    callback↔coroutine rendezvous, and the two flow-control calls — but none of
    the judgement about when to make them.  Those belong to
    :class:`BufferReader`, which is the only object that knows what has been
    asked for; this class executes what it is told.  Its one comparison — the
    byte-level high-water threshold — is a transport fact, not a judgement:
    whether a crossing pauses the peer is the Reader's call.
    """

    def __init__(self) -> None:
        self._rb = ReadBuffer()
        self.reader = BufferReader(self._rb, self)
        self.transport: asyncio.Transport | None = None
        self._waiter: asyncio.Future[None] | None = None
        self._eof = False
        self._exc: BaseException | None = None
        #: The transport is not reading.  Public because the reader consults it
        #: on the consuming path, where a property call is not free; kept a
        #: plain flag maintained by the two methods that change it.
        self.reading_paused = False
        self._drain_waiter: asyncio.Future[None] | None = None

    # -- state -------------------------------------------------------------

    @property
    def at_eof(self) -> bool:
        return self._eof

    @property
    def buffer(self) -> ReadBuffer:
        """The connection's buffer, for a successor taking the stream over."""
        return self._rb

    # -- transport callbacks ----------------------------------------------

    def connection_made(self, transport) -> None:
        self.transport = transport

    def get_buffer(self, sizehint: int) -> memoryview:
        return self._rb.get_buffer(sizehint)

    def buffer_updated(self, nbytes: int) -> None:
        if nbytes == 0:
            # asyncio treats a zero-length read as EOF on some transports.
            self.eof_received()
            return
        # The byte threshold is a transport-side fact: whether this arrival
        # crossed the high-water mark is compared here at base-equal per-arrival
        # cost.  Whether that crossing should pause — the parked-reader
        # exemption — is the reader's decision, invoked only on the crossing.
        avail = self._rb.buffer_updated(nbytes)
        if avail >= _HIGH_WATER:
            self.reader.maybe_pause()
        self._wake()

    def eof_received(self) -> bool:
        self._eof = True
        self._wake()
        # True keeps the transport open for writing.  Returning False would
        # have asyncio close it the moment the peer half-closes — and a client
        # that sends its request then calls ``shutdown(SHUT_WR)`` is doing
        # exactly that, legitimately, while still waiting for the response.
        # The write half is ours to close, once the response has shipped.
        return True

    def connection_lost(self, exc: BaseException | None) -> None:
        self._eof = True
        self._exc = exc
        self._wake()
        # A sender parked in ``drain`` waiting for the peer to read must not
        # wait for a peer that is gone: resolve it so the write path raises or
        # unwinds instead of hanging for the connection's lifetime.
        waiter, self._drain_waiter = self._drain_waiter, None
        if waiter is not None and not waiter.done():
            if exc is not None:
                waiter.set_exception(exc)
            else:
                waiter.set_result(None)

    # -- write side -------------------------------------------------------
    #
    # ``AsyncioWriter`` needs only ``write(bytes)`` + ``async drain()``, so the
    # protocol supplies them directly and the whole existing sender stack —
    # the ``_write_many`` join-vs-vectored gate, ``BB_WRITE_TIMEOUT``, the
    # deadline scanner — is reused untouched.  This is the inbound path's
    # replacement; the outbound path is already leaner than both peers.

    def write(self, data) -> None:
        if self.transport is not None:
            self.transport.write(data)

    def writelines(self, parts) -> None:
        """Vectored write — the other half of the send-path size gate.

        ``BaseSender._write_many`` joins below 32 KiB and comes here above it,
        so a protocol that offers only :meth:`write` serves small responses and
        fails large ones.  Delegated to the transport rather than joined here:
        the selector transport reaches ``sendmsg(iovec, …)`` and uvloop does a
        real vectored write, which is the entire reason the gate has an upper
        branch.
        """
        if self.transport is not None:
            self.transport.writelines(parts)

    async def drain(self) -> None:
        """Block only while the transport is over its high-water mark.

        Returns without awaiting in the common case, which matters: an
        unconditional await here is one loop turn per response send, the very
        cost the inbound rewrite is removing on the read side.
        """
        if self._exc is not None:
            raise self._exc
        if self._drain_waiter is None:
            return
        await asyncio.shield(self._drain_waiter)

    def pause_writing(self) -> None:
        if self._drain_waiter is None:
            self._drain_waiter = asyncio.get_running_loop().create_future()

    def resume_writing(self) -> None:
        waiter, self._drain_waiter = self._drain_waiter, None
        if waiter is not None and not waiter.done():
            waiter.set_result(None)

    def get_extra_info(self, name, default=None):
        if self.transport is None:
            return default
        return self.transport.get_extra_info(name, default)

    def is_closing(self) -> bool:
        return self.transport is None or self.transport.is_closing()

    def close(self) -> None:
        if self.transport is not None:
            self.transport.close()

    async def linger_close(self, max_bytes: int = 65536,
                           timeout: float = 0.25) -> None:
        """Close after briefly discarding whatever the peer is still sending.

        Closing a socket with unread bytes in its receive queue makes the
        kernel send RST, and an RST discards data we already wrote — so a peer
        that is still mid-send when we answer never sees the response.  That
        is not hypothetical: it is how a 431 for an over-budget header block
        goes missing, because rejecting at the budget means, by design, not
        reading the rest.

        nginx calls this ``lingering_close``.  Both bounds matter: reading
        without a byte cap hands an attacker the unbounded read the budget
        exists to refuse, and reading without a deadline lets a slow peer hold
        the connection open after it has been answered.

        Skipped unless we are closing with bytes we chose not to consume.  A
        completed request leaves the buffer empty, so the normal close stays a
        bare close — lingering on every connection would put a timeout on the
        teardown path that ``AsyncioWriter.close`` deliberately keeps free of
        even one extra loop turn, for the burst-keepalive workload.
        """
        if self.transport is None:
            return
        if self._eof or not self._rb.available:
            self.close()
            return
        try:
            # FIN tells the peer we are done writing, so it stops waiting for
            # more response and closes its end.
            if self.transport.can_write_eof():
                self.transport.write_eof()
            loop = asyncio.get_running_loop()
            deadline = loop.time() + timeout
            discarded = self._rb.available
            self._rb.consume(discarded)
            while not self._eof and discarded < max_bytes:
                remaining = deadline - loop.time()
                if remaining <= 0:
                    break
                try:
                    # Through the reader: discarding is still reading, so the
                    # wait has to release any pause the discarded bytes armed.
                    await asyncio.wait_for(self.reader.wait_for_data(), remaining)
                except TimeoutError:
                    break
                except Exception:
                    break
                n = self._rb.available
                self._rb.consume(n)
                discarded += n
        except Exception:
            # Teardown is best-effort: the response is already on the wire and
            # the close below is what actually matters.
            pass
        finally:
            self.close()

    # -- waiting -----------------------------------------------------------

    async def wait_for_arrival(self) -> None:
        """Park until the next arrival, EOF, or connection loss.

        The rendezvous itself — a callback↔coroutine handoff, which is why it
        stays here while the decision to wait, and the backpressure release
        that goes with it, live on :meth:`BufferReader.wait_for_data`.

        One waiter only: a connection is driven by a single actor coroutine, so
        a second concurrent reader is a bug rather than a case to support.
        """
        if self._exc is not None:
            raise self._exc
        if self._eof:
            return
        if self._waiter is not None:
            raise RuntimeError(
                'ConnectionProtocol.wait_for_arrival is not re-entrant — one '
                'connection is driven by one actor coroutine')
        self._waiter = asyncio.get_running_loop().create_future()
        try:
            await self._waiter
        finally:
            self._waiter = None
        if self._exc is not None:
            raise self._exc

    def _wake(self) -> None:
        waiter, self._waiter = self._waiter, None
        if waiter is not None and not waiter.done():
            if self._exc is not None:
                waiter.set_exception(self._exc)
            else:
                waiter.set_result(None)

    # -- flow control (execution only) -------------------------------------

    def pause_reading(self) -> None:
        """Stop the transport, because the reader asked.  Idempotent."""
        if not self.reading_paused:
            self.reading_paused = True
            if self.transport is not None:
                self.transport.pause_reading()

    def resume_reading(self) -> None:
        """Let the transport read again, because the reader asked.  Idempotent."""
        if self.reading_paused:
            self.reading_paused = False
            if self.transport is not None:
                self.transport.resume_reading()
