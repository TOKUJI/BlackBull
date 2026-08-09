"""The single owned buffer for the H/1.1 inbound path.

One `bytearray` per connection, written **directly by the kernel** through
:meth:`ReadBuffer.get_buffer` and read by cursor.  Every inbound byte is
materialised once: the head is sliced out for the parser, the body is handed
out as a `memoryview`, and a keep-alive peer's next request is simply the bytes
that were already sitting between the cursors.

This replaces reading through `asyncio.StreamReader`.  The distinction that
matters is *ownership*, not buffering — a buffer layered over a reader that is
already buffering is a third copy, which measured slower than the per-line
`readuntil` loop it was meant to beat.  The buffer only pays when it is the
only one.

Deliberately free of HTTP semantics.  It reports an over-budget head with
:data:`LIMIT_EXCEEDED` rather than raising, because the 431 belongs to the
actor; it distinguishes "EOF with nothing" from "EOF mid-head" only by leaving
:attr:`available` intact, because deciding between a silent close and a 400 is
also the actor's job.

Not thread-safe and not concurrency-safe — one connection, one buffer, one
actor loop, per the actor model.
"""
from __future__ import annotations

__all__ = ('ReadBuffer',)

#: Initial allocation.  Large enough for a typical request head plus a small
#: body without a resize, small enough that an idle keep-alive connection is
#: not holding a page per peer.
_INITIAL = 8192

#: Smallest write window offered to the transport.  This is a **memory floor
#: decision, not a throughput one**: whatever is offered here is allocated for
#: the life of every connection, idle ones included, so offering the 64 KiB a
#: `recv` could use would cost ~640 MB across 10k idle keep-alive peers.  A
#: large body simply takes more `recv` calls into a buffer that grows on
#: demand and is released again at :meth:`ReadBuffer.compact`.
_MIN_READ = 4096

#: Compact once the consumed prefix is at least this large *and* at least half
#: the buffer.  Both conditions matter: the first stops us memmoving a few
#: bytes on every request, the second stops a large resident body from being
#: shuffled repeatedly while it is being consumed.
_COMPACT_MIN = 4096


class ReadBuffer:
    """A cursor-addressed byte buffer fed by `asyncio.BufferedProtocol`."""

    __slots__ = ('_buf', '_r', '_w', '_scanned', '_eof', '_examined', '_view')

    #: :meth:`find_head_end` result meaning "the byte budget ran out before the
    #: terminator appeared".  Returned rather than raised — see module docstring.
    LIMIT_EXCEEDED = -2

    def __init__(self) -> None:
        self._buf = bytearray(_INITIAL)
        self._r = 0          # read cursor: first unconsumed byte
        self._w = 0          # write cursor: first free byte
        self._scanned = 0    # absolute offset the head scan has cleared
        self._eof = False
        self._examined = 0   # cumulative bytes the scan has looked at
        # The write window last handed to the transport.  Held so it can be
        # released before any resize: a bytearray with a live memoryview
        # raises BufferError on grow, and relying on the caller to drop its
        # reference in time makes that a load-dependent crash rather than a
        # contract.
        self._view: memoryview | None = None

    # -- state ------------------------------------------------------------

    @property
    def available(self) -> int:
        """Unconsumed bytes currently resident."""
        return self._w - self._r

    @property
    def capacity(self) -> int:
        """Size of the underlying allocation (diagnostics and tests)."""
        return len(self._buf)

    @property
    def at_eof(self) -> bool:
        return self._eof

    @property
    def examined_bytes(self) -> int:
        """Cumulative bytes the head scan has looked at on this connection.

        Exposed so the linear-scan invariant is assertable rather than merely
        intended: a scan that restarted from the front on every arrival would
        make this quadratic in the number of segments, which is a peer-chosen
        CPU cost.  Overlap of up to three bytes per resumption is expected —
        that is the straddled-terminator back-off.
        """
        return self._examined

    def feed_eof(self) -> None:
        self._eof = True

    # -- BufferedProtocol surface -----------------------------------------

    def get_buffer(self, sizehint: int) -> memoryview:
        """Space for the transport to read into.

        asyncio passes ``-1`` when it has no preference, and the protocol
        contract requires a non-empty buffer — returning an empty one stalls
        the connection permanently.
        """
        self._drop_view()
        want = _MIN_READ if sizehint <= 0 else max(sizehint, _MIN_READ)
        if len(self._buf) - self._w < want:
            self._make_room(want)
        # Hand out *all* the free space, not just what was asked for: the
        # allocation is already paid for, so a bigger window costs nothing and
        # saves `recv` calls on a large body.
        self._view = memoryview(self._buf)[self._w:]
        return self._view

    def buffer_updated(self, nbytes: int) -> None:
        """Declare how much of the last :meth:`get_buffer` was written."""
        self._w += nbytes
        self._drop_view()

    def _drop_view(self) -> None:
        """Release the outstanding write window so the buffer can be resized."""
        if self._view is not None:
            self._view.release()
            self._view = None

    # -- reading ----------------------------------------------------------

    def find_head_end(self, limit: int = 0) -> int:
        """Length of the message head, terminator included, or a sentinel.

        Returns ``-1`` when the terminator has not arrived yet and
        :data:`LIMIT_EXCEEDED` when *limit* (0 = unbounded) is passed without
        one.

        The scan resumes from where the last call stopped, backed off by three
        bytes so a ``\\r\\n\\r\\n`` split across two arrivals is still found.
        Without that resumption a peer dribbling one byte per segment makes
        every arrival re-scan the whole head — quadratic, and attacker-chosen.
        """
        start = max(self._r, self._scanned - 3)
        self._examined += self._w - start
        idx = self._buf.find(b'\r\n\r\n', start, self._w)
        if idx == -1:
            self._scanned = self._w
            if limit > 0 and self._w - self._r > limit:
                return self.LIMIT_EXCEEDED
            return -1
        # Clear up to the terminator's *start*, not past it, so asking twice
        # answers twice.  Advancing past it would make a repeat call search
        # from beyond the match and report "not found" for a head that is
        # sitting right there.
        self._scanned = idx
        end = idx + 4 - self._r
        if limit > 0 and end > limit:
            return self.LIMIT_EXCEEDED
        return end

    def find(self, sep: bytes) -> int:
        """Offset of *sep* within the resident bytes, or ``-1``.

        Unlike :meth:`find_head_end` this scans from the read cursor every
        call: its callers are the generic `readuntil` paths (chunk-size lines,
        WebSocket framing), where the search target changes between calls so a
        carried scan offset would be wrong rather than merely wasteful.
        """
        idx = self._buf.find(sep, self._r, self._w)
        return -1 if idx < 0 else idx - self._r

    def take(self, n: int) -> bytes:
        """Materialise and consume the next *n* bytes.

        The one copy per message: the head goes to the parser as `bytes`
        because the parse path's `split`/`translate` bulk ops need a real
        buffer object, and those are what keep the parser at C speed.

        *One* copy, hence the memoryview: slicing the `bytearray` directly
        would build an intermediate `bytearray` and then copy that into
        `bytes`, doubling peak memory for the duration — 2.0 MiB for a 1 MiB
        take.  Header-sized takes would not care; multi-MiB ones do, and a
        body read asks for whatever the peer declared.

        The view is released explicitly rather than left to refcounting: a
        `bytearray` with a live export raises `BufferError` on resize, and the
        next `get_buffer` may resize.  Tying that to when a temporary happens
        to be collected is how it becomes a load-dependent crash.
        """
        r = self._r
        mv = memoryview(self._buf)
        try:
            chunk = mv[r:r + n]
            try:
                out = bytes(chunk)
            finally:
                chunk.release()
        finally:
            mv.release()
        self._r = r + n
        self._reset_scan()
        return out

    def view(self, n: int) -> memoryview:
        """A view of the next *n* resident bytes — no copy, not consumed.

        Body bytes reach the application through this, so a request that is
        streamed or sent to a file never allocates a `bytes` for its payload.
        """
        return memoryview(self._buf)[self._r:self._r + n]

    def consume(self, n: int) -> None:
        """Advance past *n* bytes handed out by :meth:`view`."""
        self._r += n
        self._reset_scan()

    def compact(self) -> None:
        """Move the unconsumed tail to the front.

        Called on message boundaries.  Without it the cursors walk forward for
        the life of a keep-alive connection and the allocation grows to every
        byte ever received on it.
        """
        self._drop_view()
        r = self._r
        if r == 0:
            self._release()
            return
        if self._w == r:
            self._r = self._w = self._scanned = 0
        else:
            del self._buf[:r]
            self._buf.extend(bytes(r))       # keep the allocation, not the data
            self._w -= r
            self._scanned = max(0, self._scanned - r)
            self._r = 0
        self._release()

    def _release(self) -> None:
        """Return a grown buffer to the floor once its message is gone.

        A single large upload must not leave every connection that served one
        holding its peak allocation for the rest of its keep-alive life — that
        is the idle-memory floor the read window is also sized against.
        """
        if self._w == 0 and len(self._buf) > _INITIAL:
            self._buf = bytearray(_INITIAL)

    # -- internals --------------------------------------------------------

    def _reset_scan(self) -> None:
        """Restart the head scan for the next message.

        A scan offset carried across a message boundary starts the next search
        past that message's own terminator, so its head is never found.
        """
        self._scanned = self._r
        if self._r >= _COMPACT_MIN and self._r * 2 >= len(self._buf):
            self.compact()

    def _make_room(self, want: int) -> None:
        """Ensure *want* writable bytes, compacting before growing.

        Growth doubles rather than adding exactly what was asked for: a
        `want`-sized bump reallocates on nearly every read of a large body,
        which is O(n) memmoves over the message.
        """
        self._drop_view()
        if self._r and len(self._buf) - (self._w - self._r) >= want:
            self.compact()
            if len(self._buf) - self._w >= want:
                return
        need = self._w + want
        size = len(self._buf)
        while size < need:
            size *= 2
        self._buf.extend(bytes(size - len(self._buf)))
