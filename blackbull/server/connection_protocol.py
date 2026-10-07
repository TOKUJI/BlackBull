"""One connection buffer and transport callbacks.

BufferReader owns receive policy; ConnectionProtocol owns socket operations.
Published state has exactly one writer, its owner. See docs/about/internals.md.
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
#: [`BufferReader._at_boundary`][BufferReader._at_boundary] returns it to the floor.  The hysteresis is
#: what lets a connection repeating a large message reuse its allocation
#: instead of churning grow+shrink per message (F6 follow-up).
_RELEASE_HYSTERESIS = 4


class BufferReader(AbstractReader):
    """Read resident bytes before waiting for more.

    Own high-water pause decisions, including the starving-reader exception.
    Only one coroutine may wait for input at a time.
    """

    __slots__ = ('_buf', '_proto', '_release_count', '_waiting', 'read_offer')

    def __init__(self, buf: ReadBuffer, proto: ConnectionProtocol) -> None:
        self._buf = buf
        self._proto = proto
        #: This reader is parked waiting for bytes.  Held here rather than
        #: inferred from the protocol's rendezvous future, which is cleared on
        #: *wake* rather than on stopping waiting — an arrival in that window
        #: read as "nobody is waiting" and armed a pause the next park released.
        self._waiting = False
        #: How much this reader wants the arrival that wakes it to be able to
        #: deliver, capped at the high-water mark; 0 unless it is parked.  The
        #: protocol reads it on the arrival path and never writes it.
        self.read_offer = 0
        self._release_count = 0  # consecutive small messages since the last grow

    # -- the receive decisions ---------------------------------------------

    def maybe_pause(self) -> None:
        """A delivery crossed the high-water mark: decide whether to pause.

        Never while this reader is waiting: it is starved, not behind, so the
        condition backpressure exists to prevent is not the one in play.
        """
        if not self._waiting:
            self._proto.pause_reading()

    def _consumed(self) -> None:
        """Apply release policy after a read consumes bytes.

        Do not resolve message boundaries during arrival-side compaction: new input
        would immediately reuse the allocation.
        """
        proto, buf = self._proto, self._buf
        if proto.reading_paused and buf.available <= _LOW_WATER:
            proto.resume_reading()
        if buf.drained_boundary:
            self._at_boundary()

    def _at_boundary(self) -> None:
        """Release peak allocation only after _RELEASE_HYSTERESIS fully consumed small messages.

        A message exceeding the floor resets the count.
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
        buf.consume_boundary()

    async def wait_for_data(self) -> None:
        """Park after releasing the high-water pause.

        A starving reader must resume input even if its requested slice exceeds the
        watermark. The reader publishes read_offer around this wait; a paused
        transport cannot otherwise supply the bytes needed to unblock it.
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
            if self._proto.peer_closed:
                return b''
            # Declared around the park and nowhere else: a read the buffer can
            # already satisfy never yields, so nothing could consult the offer —
            # and on HTTP/2 that is the great majority of reads.
            self.read_offer = (
                _HIGH_WATER if n < 0 else min(n, _HIGH_WATER))
            try:
                await self.wait_for_data()
            finally:
                self.read_offer = 0
            if not self._buf.available:
                return b''
        take = self._buf.available if n < 0 else min(n, self._buf.available)
        out = self._buf.take(take)
        self._consumed()
        return out

    async def readexactly(self, n: int) -> bytes:
        # The demand is the whole read, not what is left of it: an arrival that
        # can fill the request in one recv is the point, and re-deriving a
        # smaller offer each time round would shrink the window exactly when
        # the peer is delivering slowly.
        while self._buf.available < n:
            if self._proto.peer_closed:
                partial = self._buf.take(self._buf.available)
                raise IncompleteReadError(partial)
            self.read_offer = min(n, _HIGH_WATER)
            try:
                await self.wait_for_data()
            finally:
                self.read_offer = 0
        out = self._buf.take(n)
        self._consumed()
        return out

    async def _readuntil_unbounded(self, sep: bytes) -> bytes:
        """Read through *sep* without an application byte budget."""
        scan_from = 0
        while True:
            idx = self._buf.find(sep, scan_from)
            if idx >= 0:
                end = idx + len(sep)
                out = self._buf.take(end)
                self._consumed()
                return out
            if self._proto.peer_closed:
                raise IncompleteReadError(self._buf.take(self._buf.available))
            scan_from = max(0, self._buf.available - len(sep) + 1)
            await self.wait_for_data()

    async def _readuntil_bounded(self, sep: bytes, limit: int) -> bytes:
        """Read through *sep* with a known-positive accumulation budget."""
        scan_from = 0
        while True:
            idx = self._buf.find(sep, scan_from)
            if idx >= 0:
                end = idx + len(sep)
                if end > limit:
                    raise ReadLimitExceeded(
                        f'readuntil exceeds {limit} bytes',
                        bytes(self._buf.view(min(self._buf.available,
                                                 limit + len(sep)))))
                out = self._buf.take(end)
                self._consumed()
                return out
            if self._buf.available > limit:
                raise ReadLimitExceeded(
                    f'readuntil exceeds {limit} bytes',
                    bytes(self._buf.view(min(self._buf.available,
                                             limit + len(sep)))))
            if self._proto.peer_closed:
                raise IncompleteReadError(self._buf.take(self._buf.available))
            scan_from = max(0, self._buf.available - len(sep) + 1)
            await self.wait_for_data()

    async def discard(self, max_bytes: int, deadline: float) -> int:
        """Read and throw away up to *max_bytes*, until EOF or *deadline*.

        The read side of the lingering close.  Both bounds are the caller's to
        set and neither is optional; ``docs/about/internals.md`` §Rejecting
        requires lingering says what each one refuses.

        Returns the number of bytes discarded.  Errors are the caller's to
        absorb: this runs on the teardown path, where the response is already
        on the wire.
        """
        loop = asyncio.get_running_loop()
        discarded = self._buf.available
        self._buf.consume(discarded)
        self._consumed()
        while not self._proto.peer_closed and discarded < max_bytes:
            remaining = deadline - loop.time()
            if remaining <= 0:
                break
            try:
                await asyncio.wait_for(self.wait_for_data(), remaining)
            except (TimeoutError, Exception):
                break
            n = self._buf.available
            self._buf.consume(n)
            discarded += n
            self._consumed()
        return discarded

    def has_buffered(self) -> bool:
        return self._buf.available > 0

    def buffered_len(self) -> int:
        return self._buf.available

    def peek(self, n: int | None = None) -> bytes:
        avail = self._buf.available
        want = avail if n is None else min(n, avail)
        return bytes(self._buf.view(want))

    def at_eof(self) -> bool:
        return self._proto.peer_closed and not self._buf.available

    async def fill(self, n: int) -> bool:
        """Wait until *n* bytes are resident, consuming nothing."""
        while self._buf.available < n:
            if self._proto.peer_closed:
                return False
            await self.wait_for_data()
        return True

    # -- the one-scan header read ------------------------------------------

    async def _read_head_unbounded(self) -> bytes:
        """[`read_head`][] with no byte budget."""
        while True:
            if not self._buf.available:
                if self._proto.peer_closed:
                    return b''
                await self.wait_for_data()
                continue
            end = self._buf.find_head_end()
            if end >= 0:
                out = self._buf.take(end)
                self._consumed()
                return out
            if self._proto.peer_closed:
                partial = self._buf.take(self._buf.available)
                if not partial:
                    return b''
                raise IncompleteReadError(partial)
            await self.wait_for_data()

    async def _read_head_bounded(self, limit: int) -> bytes:
        """[`read_head`][] under a byte budget, found in one scan.

        Contract as documented on [`AbstractReader.read_head`][AbstractReader.read_head] — an idle
        close returns ``b''`` and a truncated one raises with the partial.
        """
        while True:
            if not self._buf.available:
                # An empty buffer cannot exceed a positive limit; wait before scanning.
                if self._proto.peer_closed:
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
            if self._proto.peer_closed:
                partial = self._buf.take(self._buf.available)
                if not partial:
                    return b''
                raise IncompleteReadError(partial)
            await self.wait_for_data()


class ConnectionProtocol(asyncio.BufferedProtocol):
    """Own transport callbacks and wakeups for one connection.

    BufferReader decides pause/resume policy; this object applies it.
    """

    def __init__(self) -> None:
        self._rb = ReadBuffer()
        self.reader = BufferReader(self._rb, self)
        self.transport: asyncio.Transport | None = None
        self._waiter: asyncio.Future[None] | None = None
        self._eof = False
        self._exc: BaseException | None = None
        self._response_written = False
        self._arrived = False
        self._linger_decided = False
        # Cleartext until connection_made says otherwise.
        self._half_close_is_honoured = True
        #: The transport is not reading.  Written only by [`pause_reading`][]
        #: / [`resume_reading`][]; the reader polls it on the consuming path.
        self.reading_paused = False
        self._drain_waiter: asyncio.Future[None] | None = None

    # -- state -------------------------------------------------------------

    @property
    def peer_closed(self) -> bool:
        """Return transport EOF; buffered bytes may still remain, so this differs from reader.at_eof.
        """
        return self._eof

    # -- transport callbacks ----------------------------------------------

    def connection_made(self, transport) -> None:
        self.transport = transport
        # Whether a half-close can leave the write half open, resolved once —
        # see [`eof_received`][].
        self._half_close_is_honoured = (
            transport.get_extra_info('ssl_object') is None)

    def get_buffer(self, sizehint: int) -> memoryview:
        # Read demand from the reader-owned published field, not the size hint.
        return self._rb.get_buffer(sizehint, want=self.reader.read_offer)

    def buffer_updated(self, nbytes: int) -> None:
        if nbytes == 0:
            # asyncio treats a zero-length read as EOF on some transports.
            self.eof_received()
            return
        self._arrived = True
        # The threshold is a transport fact; what a crossing *means* is the
        # reader's, so it is asked only on the crossing.
        avail = self._rb.buffer_updated(nbytes)
        if avail >= _HIGH_WATER:
            self.reader.maybe_pause()
        self._wake()

    def eof_received(self) -> bool:
        self._eof = True
        self._wake()
        # Cleartext half-close keeps output open until the response is sent.
        # TLS transports close on EOF regardless of this return value.
        return self._half_close_is_honoured

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
    # protocol supplies them directly and the whole sender stack — the
    # ``_write_many`` gate, ``BB_WRITE_TIMEOUT``, the deadline scanner — is
    # reused unchanged.

    def write(self, data) -> None:
        if data:
            self._response_written = True
        if self.transport is not None:
            self.transport.write(data)

    def writelines(self, parts) -> None:
        """Vectored write — the upper branch of the send-path size gate.

        Delegated to the transport, never joined here.  A backing object that
        offers only ``write`` serves small responses and fails large ones;
        ``docs/about/internals.md`` §Send-path invariant is the obligation.
        """
        if any(parts):
            self._response_written = True
        if self.transport is not None:
            self.transport.writelines(parts)

    async def drain(self) -> None:
        """Wait only while the transport is above its write high-water mark.
        """
        if self._exc is not None:
            raise self._exc
        if self._drain_waiter is None:
            return
        await asyncio.shield(self._drain_waiter)

    @property
    def writing_paused(self) -> bool:
        """Whether [`drain`][] would wait."""
        return self._drain_waiter is not None

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

        Reads and discards up to *max_bytes* for at most *timeout* seconds,
        then closes.  A connection that wrote a response and may still have
        unread bytes — resident, or nothing arrived and no EOF — lingers
        once; otherwise its buffer and EOF decide.
        The Internals page explains why a rejection has to close this way and
        why both bounds are needed.  nginx calls it ``lingering_close``.
        """
        if self.transport is None:
            return
        if self._linger_decided:
            self.close()
            return
        self._linger_decided = True
        owed = (self._response_written
                and (self._rb.available or not (self._arrived or self._eof)))
        if not owed and (self._eof or not self._rb.available):
            self.close()
            return
        try:
            # FIN tells the peer we are done writing, so it stops waiting and
            # closes its end.  Discarding what it still sends is *reading*, so
            # that half belongs to the reader, bounds and all.
            if self.transport.can_write_eof():
                self.transport.write_eof()
            loop = asyncio.get_running_loop()
            await self.reader.discard(max_bytes, loop.time() + timeout)
        except Exception:
            pass  # best-effort: the response is on the wire; the close matters
        finally:
            self.close()

    # -- waiting -----------------------------------------------------------

    async def wait_for_arrival(self) -> None:
        """Park until the next arrival, EOF, or connection loss.

        The bare rendezvous; the decision to wait, and the backpressure release
        that goes with it, are [`BufferReader.wait_for_data`][BufferReader.wait_for_data]'s.

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
