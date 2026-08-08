"""H/1.1 transport front end: `asyncio.BufferedProtocol` over one owned buffer.

This is what replaces `asyncio.StreamReader` on the H/1.1 inbound path.  The
kernel writes straight into the connection's :class:`~.read_buffer.ReadBuffer`
through :meth:`H1Protocol.get_buffer`, and the actor's coroutine parks on a
future that :meth:`H1Protocol.buffer_updated` resolves.

**The actor invariant is intact.**  One coroutine still owns the connection's
state and processes one request at a time; it is woken by the protocol instead
of parking inside `readuntil`.  That distinction is the whole design: sanic
reaches half our read/parse cost with a parked coroutine too, so the cost was
never the coroutine — it was reading through a second buffer.

:class:`BufferReader` presents the :class:`~.recipient.AbstractReader` surface
so the body recipient and the WebSocket/h2c successors work unchanged, and adds
:meth:`BufferReader.read_head` — the one-scan header read the actor uses
instead of a `readuntil` per line.
"""
from __future__ import annotations

import asyncio

from .read_buffer import ReadBuffer
from .recipient import AbstractReader, IncompleteReadError

__all__ = ('BufferReader', 'H1Protocol', 'HeadTooLargeError')

#: Stop reading from the transport once this many unconsumed bytes are resident.
#: Backpressure's memory half: without it a fast peer feeding a slow handler
#: grows the buffer without bound.  Matched to the write-side watermark shape.
_HIGH_WATER = 128 * 1024

#: Resume once the buffer falls back to this.  A gap between the two stops the
#: transport being paused and resumed on alternate reads.
_LOW_WATER = 32 * 1024


class HeadTooLargeError(Exception):
    """The message head exceeded the caller's budget.

    Distinct from the buffer's `LIMIT_EXCEEDED` sentinel because the actor
    answers 431 for it; the buffer itself owns no HTTP semantics.
    """


class BufferReader(AbstractReader):
    """`AbstractReader` over a :class:`ReadBuffer` fed by :class:`H1Protocol`.

    Every method serves from resident bytes first and only parks when it needs
    more.  A pipelined or keep-alive peer's next head is usually already
    resident, so those reads complete without a loop turn — which is the claim
    the layered predecessor made and could not deliver, because it sat on a
    reader that was buffering underneath it.
    """

    __slots__ = ('_buf', '_proto')

    def __init__(self, buf: ReadBuffer, proto: 'H1Protocol') -> None:
        self._buf = buf
        self._proto = proto

    # -- AbstractReader ----------------------------------------------------

    async def read(self, n: int = -1) -> bytes:
        if not self._buf.available:
            if self._proto.at_eof:
                return b''
            await self._proto.wait_for_data()
            if not self._buf.available:
                return b''
        take = self._buf.available if n < 0 else min(n, self._buf.available)
        out = self._buf.take(take)
        self._proto.maybe_resume()
        return out

    async def readexactly(self, n: int) -> bytes:
        while self._buf.available < n:
            if self._proto.at_eof:
                partial = self._buf.take(self._buf.available)
                raise IncompleteReadError(partial)
            await self._proto.wait_for_data()
        out = self._buf.take(n)
        self._proto.maybe_resume()
        return out

    async def readuntil(self, sep: bytes = b'\n') -> bytes:
        while True:
            idx = self._buf.find(sep)
            if idx >= 0:
                out = self._buf.take(idx + len(sep))
                self._proto.maybe_resume()
                return out
            if self._proto.at_eof:
                raise IncompleteReadError(self._buf.take(self._buf.available))
            await self._proto.wait_for_data()

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

    # -- the one-scan header read ------------------------------------------

    async def read_head(self, limit: int) -> bytes:
        """The message head, terminator included.

        Returns ``b''`` at EOF without a complete head — the caller
        distinguishes an idle close from a truncated one by whether bytes are
        still buffered, exactly as the streams path did via
        ``IncompleteReadError``'s partial.  Raises :class:`HeadTooLargeError`
        past *limit*.
        """
        while True:
            end = self._buf.find_head_end(limit=limit)
            if end == ReadBuffer.LIMIT_EXCEEDED:
                raise HeadTooLargeError(
                    f'header block exceeds {limit} bytes')
            if end >= 0:
                out = self._buf.take(end)
                self._proto.maybe_resume()
                return out
            if self._proto.at_eof:
                return b''
            await self._proto.wait_for_data()


class H1Protocol(asyncio.BufferedProtocol):
    """Buffered-protocol front end for one H/1.1 connection."""

    def __init__(self) -> None:
        self._rb = ReadBuffer()
        self.reader = BufferReader(self._rb, self)
        self.transport: asyncio.Transport | None = None
        self._waiter: asyncio.Future[None] | None = None
        self._eof = False
        self._exc: BaseException | None = None
        self._paused = False

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
        self._rb.buffer_updated(nbytes)
        if not self._paused and self._rb.available >= _HIGH_WATER:
            # Backpressure: stop the kernel handing us more until the handler
            # catches up.  Paired with ``maybe_resume`` on every consuming read.
            self._paused = True
            if self.transport is not None:
                self.transport.pause_reading()
        self._wake()

    def eof_received(self) -> bool:
        self._eof = True
        self._wake()
        # False: let asyncio close the transport.  A half-open connection buys
        # nothing here — the response path writes through the same transport,
        # and every framing decision is already made from what arrived.
        return False

    def connection_lost(self, exc: BaseException | None) -> None:
        self._eof = True
        self._exc = exc
        self._wake()

    def pause_writing(self) -> None:      # pragma: no cover - transport-driven
        pass

    def resume_writing(self) -> None:     # pragma: no cover - transport-driven
        pass

    # -- waiting -----------------------------------------------------------

    async def wait_for_data(self) -> None:
        """Park until the next arrival, EOF, or connection loss.

        One waiter only: a connection is driven by a single actor coroutine, so
        a second concurrent reader is a bug rather than a case to support.
        """
        if self._exc is not None:
            raise self._exc
        if self._eof:
            return
        if self._waiter is not None:
            raise RuntimeError(
                'H1Protocol.wait_for_data is not re-entrant — one connection '
                'is driven by one actor coroutine')
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

    def maybe_resume(self) -> None:
        """Resume reading once the resident bytes fall back to the low mark."""
        if self._paused and self._rb.available <= _LOW_WATER:
            self._paused = False
            if self.transport is not None:
                self.transport.resume_reading()
