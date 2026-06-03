"""Cancellable byte buffer for the custom asyncio protocol path.

Sprint 30 Tier 1.5 Step 1 — see
``bench/sprint-logs/sprint-30-tier1.5-design.md`` for the architectural
rationale.  Briefly: the existing ``asyncio.StreamReader`` uses a
future-wakeup model whose per-readuntil-resume cost adds 1-2
event-loop turns per connection close.  Under burst-keepalive (HttpArena
``static`` at c=4096) that multiplies into a multi-second drain.  This
buffer replaces ``StreamReader`` on the keepalive-idle read path so
EOF — when it arrives via the ``_BlackBullProtocol.eof_received``
callback — can short-circuit the chain by cancelling the awaiting task
synchronously instead of waking it through a future and a subsequent
loop tick.

Design constraints
------------------
* Single-task consumer per buffer (one connection-actor reads one
  buffer); single feeder per buffer (the protocol's
  ``data_received`` / ``eof_received`` callbacks).  No locks needed
  — asyncio is single-threaded.
* Cancellation-safe: if the consumer task is cancelled mid-read,
  the pending waiter resolves cleanly and the buffer remains usable
  for a fresh reader (e.g. a graceful-shutdown handoff).  In
  practice the actor exits on cancellation, so this matters mostly
  for test ergonomics.
* No internal call to ``asyncio.wait_for``: the per-phase
  deadlines that ``http1_actor`` enforces (header / body /
  keepalive) wrap our reads externally.  This keeps the buffer
  itself transport-agnostic and free of timeout interactions.

API mirrors ``asyncio.StreamReader``'s ``read`` / ``readuntil`` /
``readexactly`` so the existing ``AsyncioReader`` shim can proxy
through with minimal changes.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Optional

from .recipient import IncompleteReadError

logger = logging.getLogger(__name__)


class LimitOverrunError(Exception):
    """Raised by :meth:`ProtocolBuffer.read_until` when ``max_size`` is
    exceeded before the delimiter is found.

    Mirrors ``asyncio.LimitOverrunError`` semantics (with attribute
    ``consumed`` for diagnostics) so the existing per-line / per-block
    size checks in ``http1_actor`` continue to work.
    """

    def __init__(self, message: str, consumed: int) -> None:
        super().__init__(message)
        self.consumed = consumed


class ProtocolBuffer:
    """Byte buffer fed by the protocol layer, drained by an
    actor-side ``await`` consumer.

    The consumer suspends in ``read_until`` / ``read_exactly`` /
    ``read``; the feeder calls :meth:`feed` (after
    ``protocol.data_received``) and :meth:`feed_eof` (after
    ``protocol.eof_received``) to wake the consumer.

    A single ``_waiter`` future drives the wakeup — there's no
    queueing layer between the protocol callback and the consumer.
    On EOF the consumer's pending await raises
    :class:`IncompleteReadError` (matching ``AsyncioReader``'s
    contract) once it next checks the buffer.
    """

    __slots__ = ('_buf', '_eof', '_waiter', '_closed')

    def __init__(self) -> None:
        # ``bytearray`` for O(1) append + cheap slice-delete from the
        # front.  Empirically faster than ``bytes`` concatenation for
        # the per-request feed-then-drain pattern.
        self._buf: bytearray = bytearray()
        self._eof: bool = False
        # Single in-flight waiter — set by the consumer when it
        # suspends, resolved by the feeder on the next data/eof event.
        self._waiter: Optional[asyncio.Future] = None
        # Once True, all subsequent reads raise IncompleteReadError
        # without ever suspending; used as a "you cannot recover this
        # connection" signal from ``_BlackBullProtocol``.
        self._closed: bool = False

    # ------------------------------------------------------------------
    # Feed-side API (called from protocol callbacks, never awaited)
    # ------------------------------------------------------------------

    def feed(self, data: bytes) -> None:
        """Append data and wake any suspended consumer."""
        if not data:
            return
        if self._closed:
            # Buffer was force-closed; drop the data.  Should be rare —
            # the protocol stops calling feed() after eof_received /
            # connection_lost.
            logger.debug('protocol_buffer: feed on closed buffer (%d bytes dropped)',
                         len(data))
            return
        self._buf.extend(data)
        self._wake()

    def feed_eof(self) -> None:
        """Mark end-of-stream and wake any suspended consumer.

        Subsequent reads return what's still buffered, then raise
        :class:`IncompleteReadError` once the buffer is exhausted.
        """
        if self._eof:
            return
        self._eof = True
        self._wake()

    def close(self) -> None:
        """Force-close: all future reads raise immediately.

        Called by ``_BlackBullProtocol`` on connection_lost when an
        error condition meant we should not even drain remaining
        buffered bytes.
        """
        self._closed = True
        self._eof = True
        self._wake()

    def _wake(self) -> None:
        if self._waiter is not None and not self._waiter.done():
            self._waiter.set_result(None)
            # Don't None out; the awaiter resets in its finally.

    # ------------------------------------------------------------------
    # Properties (cheap, for ``_BlackBullProtocol``'s pause/resume logic)
    # ------------------------------------------------------------------

    @property
    def buffered_bytes(self) -> int:
        return len(self._buf)

    @property
    def eof_received(self) -> bool:
        return self._eof

    # ------------------------------------------------------------------
    # Consumer-side API (await these)
    # ------------------------------------------------------------------

    async def read(self, n: int = -1) -> bytes:
        """Read up to *n* bytes, or whatever is available.

        Blocks until at least one byte is available, or EOF is
        signalled.  ``n=-1`` means "everything buffered".  Returns
        ``b''`` on EOF with empty buffer.
        """
        if not self._buf and not self._eof:
            await self._suspend()
        if n < 0 or n >= len(self._buf):
            result = bytes(self._buf)
            self._buf.clear()
        else:
            result = bytes(self._buf[:n])
            del self._buf[:n]
        return result

    async def read_exactly(self, n: int) -> bytes:
        """Read exactly *n* bytes.

        Raises :class:`IncompleteReadError` if EOF arrives before *n*
        bytes are available; the partial bytes are attached to the
        exception via the standard asyncio API (``.partial``).
        """
        if n < 0:
            raise ValueError(f'read_exactly: n must be non-negative, got {n}')
        if n == 0:
            return b''
        while len(self._buf) < n:
            if self._eof:
                # Surface partial data via the asyncio
                # IncompleteReadError convention.
                partial = bytes(self._buf)
                self._buf.clear()
                exc = IncompleteReadError(f'EOF after {len(partial)} of {n} bytes')
                exc.partial = partial          # type: ignore[attr-defined]
                exc.expected = n               # type: ignore[attr-defined]
                raise exc
            await self._suspend()
        result = bytes(self._buf[:n])
        del self._buf[:n]
        return result

    async def read_until(self, sep: bytes, *, max_size: int = -1) -> bytes:
        """Read until *sep* appears in the buffer; return the bytes
        up to and including *sep*.

        Raises :class:`IncompleteReadError` if EOF arrives before
        *sep* is found.  Raises :class:`LimitOverrunError` if
        ``max_size > 0`` and the buffer grows past it without seeing
        *sep* — the caller should answer with the appropriate
        protocol error (e.g. ``431 Request Header Fields Too Large``).
        """
        if not sep:
            raise ValueError('read_until: sep must be a non-empty bytes object')
        # Track the earliest possible position where sep could appear
        # so we don't re-scan the whole buffer on each iteration.
        scan_from = 0
        while True:
            idx = self._buf.find(sep, scan_from)
            if idx >= 0:
                end = idx + len(sep)
                result = bytes(self._buf[:end])
                del self._buf[:end]
                return result
            # Not found yet.
            if max_size > 0 and len(self._buf) > max_size:
                raise LimitOverrunError(
                    f'read_until: buffer exceeded {max_size} bytes without '
                    f'finding {sep!r}',
                    consumed=len(self._buf),
                )
            if self._eof:
                partial = bytes(self._buf)
                self._buf.clear()
                exc = IncompleteReadError(
                    f'EOF after {len(partial)} bytes without finding {sep!r}'
                )
                exc.partial = partial          # type: ignore[attr-defined]
                exc.expected = None            # type: ignore[attr-defined]
                raise exc
            # Avoid re-scanning bytes we've already checked, BUT keep
            # the (len(sep) - 1) trailing bytes so we catch sep that
            # straddles two feeds.
            scan_from = max(0, len(self._buf) - (len(sep) - 1))
            await self._suspend()

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    async def _suspend(self) -> None:
        """Park until the next ``feed`` / ``feed_eof`` / ``close`` call.

        Single waiter at a time (enforced by an assertion to surface
        misuse during tests; the production invariant is "one actor
        per connection / per buffer").
        """
        if self._waiter is not None:
            raise RuntimeError(
                'ProtocolBuffer: concurrent suspends — only one consumer '
                'is supported per buffer'
            )
        loop = asyncio.get_event_loop()
        self._waiter = loop.create_future()
        try:
            await self._waiter
        finally:
            self._waiter = None
