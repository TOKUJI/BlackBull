"""Buffer-owning H/1.1 read front end (``BB_H1_PROTOCOL``).

Phase 1a of the H/1.1 protocol-mode fast path.  The eventual front end is an
:class:`asyncio.Protocol` whose ``data_received`` appends straight into a
``bytearray``; this module implements the buffer half of that design *now*,
layered over the existing reader, so the header scan, the pushback semantics,
and the keep-alive residency can be built and proven before the transport is
rewired.  The buffer logic here moves to the protocol unchanged.

**Why the scan needs to own a buffer.** The obvious cheap version of this
phase — keep ``StreamReader`` and swap the per-line ``readuntil(b'\\r\\n')``
loop for one ``readuntil(b'\\r\\n\\r\\n')`` — does not work, twice over:

1. ``readuntil`` on a *stream* cannot see bytes the protocol-detect stage
   already moved into the actor's ``_request``.  A minimal
   ``GET / HTTP/1.0\\r\\n\\r\\n`` leaves only the terminating ``\\r\\n`` on the
   stream, and a search for the four-byte delimiter blocks until the peer
   closes.  That deadlock is why the line-by-line loop exists at all.  Scanning
   an *accumulated* buffer that starts with the already-consumed bytes has no
   such blind spot.
2. Any scan that reads in chunks will over-read — past the header block into
   the body, or into the next pipelined request.  Line-by-line never
   over-reads, which is precisely why it cannot be replaced without somewhere
   to put the surplus.  The buffer is that somewhere: leftover stays put and
   the next read is served from it.

Point 2 is also where the performance comes from.  A keep-alive or pipelined
peer usually has the next request head already in the buffer, and a coroutine
that returns without awaiting does not yield to the loop — so those requests
cost **zero suspensions**, against one loop turn per line today.
"""
from __future__ import annotations

from .recipient import AbstractReader, IncompleteReadError

__all__ = ('BufferedH1Reader', 'LIMIT_EXCEEDED')

#: ``fill_until`` result meaning "the byte budget ran out before the delimiter
#: appeared".  Returned rather than raised so this module stays free of HTTP
#: semantics — the caller owns the 431 that follows.
LIMIT_EXCEEDED = -2

# Read granularity.  Large enough that a typical head arrives in one call,
# small enough not to hand a slow-loris peer a free 64 KiB allocation per
# wakeup.  The protocol front end will not need this at all — the transport
# decides chunk size there.
_CHUNK = 65536


class BufferedH1Reader:
    """Reader that owns its unconsumed bytes.

    Wraps any object exposing ``read`` / ``readuntil`` / ``readexactly`` and
    presents the same three methods, so it is a drop-in for the
    ``AbstractReader`` the H/1.1 actor and its recipient already take.  The
    difference is the buffer: bytes read ahead of what a caller asked for are
    kept, not discarded, and every method serves from the buffer before
    touching the underlying reader.

    Not thread-safe and not concurrency-safe — one connection, one reader, one
    actor loop, per the actor model.
    """

    __slots__ = ('_reader', '_buf', '_eof')

    def __init__(self, reader) -> None:
        self._reader = reader
        self._buf = bytearray()
        self._eof = False

    # -- buffer inspection (used by the single-scan header read) -----------

    @property
    def buffered(self) -> int:
        """Bytes currently held, unconsumed."""
        return len(self._buf)

    def peek(self) -> bytes:
        """The buffered bytes, without consuming them."""
        return bytes(self._buf)

    def unread(self, data: bytes) -> None:
        """Push *data* back to the front of the buffer.

        The seam that makes over-reading safe: the header scan hands back
        whatever followed the header block, and the body reader or the next
        keep-alive iteration picks it up transparently.
        """
        if data:
            self._buf[:0] = data

    async def _fill(self) -> int:
        """Pull one chunk from the underlying reader.  Returns bytes added."""
        if self._eof:
            return 0
        chunk = await self._reader.read(_CHUNK)
        if not chunk:
            self._eof = True
            return 0
        self._buf += chunk
        return len(chunk)

    async def fill_until(self, sep: bytes, start: int = 0, limit: int = 0) -> int:
        """Read until *sep* appears at or after offset *start*.

        Returns the index of *sep*, ``-1`` at EOF without a match, or
        :data:`LIMIT_EXCEEDED` when *limit* (0 = unbounded) is passed without
        one.  The limit is not optional in practice: without it a peer that
        never sends the delimiter grows the buffer until the process dies,
        which is the slow-loris shape the header budget exists to bound.

        Rescans only from ``len(buf) - len(sep) + 1`` on each new chunk, so a
        delimiter split across two reads is still found without re-scanning the
        whole buffer — the quadratic trap in a naive "search it all again" loop.
        """
        idx = self._buf.find(sep, start)
        while idx == -1:
            if limit > 0 and len(self._buf) > limit:
                return LIMIT_EXCEEDED
            scan_from = max(start, len(self._buf) - len(sep) + 1)
            if await self._fill() == 0:
                return -1
            idx = self._buf.find(sep, scan_from)
        return idx

    # -- AbstractReader surface --------------------------------------------

    # Each method below delegates outright when the buffer is empty.  That is
    # the common case once the head is consumed — a body read of any size, and
    # every read on a connection with no surplus — and delegating keeps the
    # underlying reader's exact semantics (its EOF timing, its error types, its
    # own buffering) instead of re-deriving them here.  It also means a large
    # body is never copied into this buffer just to be copied straight out.
    # Interposition happens only while there is surplus to serve first, which
    # is exactly the window that needs it.

    async def read(self, n: int = -1) -> bytes:
        if not self._buf:
            return await self._reader.read(n)
        if n < 0:
            out, self._buf = bytes(self._buf), bytearray()
            return out + await self._reader.read(-1)
        out = bytes(self._buf[:n])
        del self._buf[:n]
        return out

    async def readuntil(self, sep: bytes = b'\n') -> bytes:
        if not self._buf:
            return await self._reader.readuntil(sep)
        idx = await self.fill_until(sep)
        if idx < 0:
            partial = bytes(self._buf)
            self._buf = bytearray()
            raise IncompleteReadError(partial)
        end = idx + len(sep)
        out = bytes(self._buf[:end])
        del self._buf[:end]
        return out

    async def readexactly(self, n: int) -> bytes:
        if not self._buf:
            return await self._reader.readexactly(n)
        if len(self._buf) >= n:
            out = bytes(self._buf[:n])
            del self._buf[:n]
            return out
        head, self._buf = bytes(self._buf), bytearray()
        return head + await self._reader.readexactly(n - len(head))

    def at_eof(self) -> bool:
        return self._eof and not self._buf

    def has_buffered(self) -> bool:
        return bool(self._buf) or self._reader.has_buffered()

    def buffered_len(self) -> int:
        return len(self._buf) + self._reader.buffered_len()

    def peek(self, n: int) -> bytes:
        return (bytes(self._buf) + self._reader.peek(n))[:n]

    def __getattr__(self, name):
        # Anything else the underlying reader offers (feed_eof, transport
        # pokes in tests) passes through unchanged.
        return getattr(self._reader, name)


# Registered rather than subclassed.  ``RecipientFactory`` wraps any reader
# that fails this check in an ``AsyncioReader``, and it builds a recipient per
# request — so without registration the front end whose whole purpose is to
# remove per-request work would add an allocation to every request.  The class
# already implements the full surface (including ``at_eof``), so inheritance
# would contribute nothing but a ``__dict__``: ``AbstractReader`` declares no
# ``__slots__``, and a base without them makes a subclass's ``__slots__`` a
# silent no-op.
AbstractReader.register(BufferedH1Reader)
