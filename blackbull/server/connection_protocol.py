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


class BufferReader(AbstractReader):
    """`AbstractReader` over a :class:`ReadBuffer` fed by :class:`ConnectionProtocol`.

    Every method serves from resident bytes first and only parks when it needs
    more.  A pipelined or keep-alive peer's next head is usually already
    resident, so those reads complete without a loop turn — which is the claim
    the layered predecessor made and could not deliver, because it sat on a
    reader that was buffering underneath it.
    """

    __slots__ = ('_buf', '_proto')

    def __init__(self, buf: ReadBuffer, proto: 'ConnectionProtocol') -> None:
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

    async def fill(self, n: int) -> bool:
        """Wait until *n* bytes are resident, consuming nothing.

        Free here: resident bytes are already the buffer's normal state, so
        peeking is just not calling ``take``.  It is the reason detection can
        hand the winning binding this very reader with the stream still whole.
        """
        while self._buf.available < n:
            if self._proto.at_eof:
                return False
            await self._proto.wait_for_data()
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
            end = self._buf.find_head_end(limit=limit)
            if end == ReadBuffer.LIMIT_EXCEEDED:
                # The bytes stay resident: the caller classifies them, and the
                # lingering close still has something to discard.
                raise ReadLimitExceeded(
                    f'head exceeds {limit} bytes',
                    bytes(self._buf.view(min(self._buf.available, limit + 2))))
            if end >= 0:
                out = self._buf.take(end)
                self._proto.maybe_resume()
                return out
            if self._proto.at_eof:
                partial = self._buf.take(self._buf.available)
                if not partial:
                    return b''
                raise IncompleteReadError(partial)
            await self._proto.wait_for_data()


class ConnectionProtocol(asyncio.BufferedProtocol):
    """Buffered-protocol front end for one H/1.1 connection."""

    def __init__(self) -> None:
        self._rb = ReadBuffer()
        self.reader = BufferReader(self._rb, self)
        self.transport: asyncio.Transport | None = None
        self._waiter: asyncio.Future[None] | None = None
        self._eof = False
        self._exc: BaseException | None = None
        self._paused = False
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
                    await asyncio.wait_for(self.wait_for_data(), remaining)
                except (asyncio.TimeoutError, TimeoutError):
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
                'ConnectionProtocol.wait_for_data is not re-entrant — one connection '
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
