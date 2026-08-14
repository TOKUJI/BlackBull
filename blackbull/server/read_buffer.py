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

Free of *receive* policy for the same reason.  It grows on demand, reports a
drained message boundary, tracks the message's peak resident bytes (accounting
— the release *policy* consumes it), and offers :meth:`release_to_floor` — but
when the allocation is actually given back is the reader's call, because only
the reader knows what the connection has been asked to deliver.

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
#: demand and is released again at :meth:`ReadBuffer.release_to_floor`.
_MIN_READ = 4096

#: Compact once the consumed prefix is at least this large *and* at least half
#: the buffer.  Both conditions matter: the first stops us memmoving a few
#: bytes on every request, the second stops a large resident body from being
#: shuffled repeatedly while it is being consumed.
_COMPACT_MIN = 4096

#: Size at or above which :meth:`ReadBuffer.take` copies through a memoryview
#: instead of a `bytearray` slice.  Measured crossover is between 8 and 16 KiB
#: (see the table on ``take``); 8 KiB puts a request head — which every request
#: pays — on the cheaper side, and every realistic body read on the other.
#: Deliberately not configurable: it is a property of the interpreter's copy
#: costs, not of a deployment.
_VIEW_COPY_THRESHOLD = 8192


class ReadBuffer:
    """A cursor-addressed byte buffer fed by `asyncio.BufferedProtocol`."""

    __slots__ = (
        '_buf',
        '_eof',
        '_examined',
        '_r',
        '_scanned',
        '_view',
        '_w',
        'drained_boundary',
        'grown',
        'peak_avail',
    )

    #: :meth:`find_head_end` result meaning "the byte budget ran out before the
    #: terminator appeared".  Returned rather than raised — see module docstring.
    LIMIT_EXCEEDED = -2

    #: The allocation a buffer sits at until a message makes it grow, and the
    #: size :meth:`release_to_floor` returns it to.  Public because the release
    #: *policy* lives on the reader: it compares a message's peak against this.
    FLOOR = _INITIAL

    def __init__(self) -> None:
        self._buf = bytearray(_INITIAL)
        self._r = 0          # read cursor: first unconsumed byte
        self._w = 0          # write cursor: first free byte
        self._scanned = 0    # absolute offset the head scan has cleared
        self._eof = False
        self._examined = 0   # cumulative bytes the scan has looked at
        #: Allocation is above the floor.  A plain flag rather than a
        #: ``capacity > FLOOR`` comparison because the reader consults it on
        #: every arrival and every consuming read, while it changes only at the
        #: two sites that resize.
        self.grown = False
        #: A compaction left the buffer empty — the message boundary the
        #: reader's release policy hangs off.  Raised here, cleared by whoever
        #: acts on it; the buffer itself never reads it.
        self.drained_boundary = False
        #: Peak resident bytes since the last boundary.  Accounting, not
        #: policy: tracked here because the write path already holds the
        #: cursors, and consumed (reset) by the reader's release hysteresis.
        self.peak_avail = 0
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

        The *sizehint* is what the transport would *like* to read in one
        recv, not what it needs: uvloop's cleartext path passes libuv's fixed
        64 KiB on every call, so honouring it would grow every connection's
        buffer to 64 KiB on its first request and the reader's release policy
        would give it back at the message boundary — a 64 KiB alloc/free
        churn per request (the F5 read-path finding).  Growth is driven by
        bytes actually arriving (the ``_w`` cursor), never by the hint: offer
        the buffer's free span, growing only when it falls below the read
        floor.
        """
        self._drop_view()
        if len(self._buf) - self._w < _MIN_READ:
            self._make_room(_MIN_READ)
        # Hand out *all* the free space, not just what was asked for: the
        # allocation is already paid for, so a bigger window costs nothing and
        # saves `recv` calls on a large body.
        self._view = memoryview(self._buf)[self._w:]
        return self._view

    def buffer_updated(self, nbytes: int) -> int:
        """Declare how much of the last :meth:`get_buffer` was written.

        Returns the new resident count so the caller — the reader's arrival
        decision — need not re-read it.  Tracks the message's peak resident
        bytes while it is at it, but only once the buffer has grown past the
        floor; a floor-sized buffer can never need the hysteresis decision,
        so the common case costs one compare.
        """
        self._w += nbytes
        if self.grown and self._w - self._r > self.peak_avail:
            self.peak_avail = self._w - self._r
        # uvloop can still hold the window's export here (see _drop_view);
        # only this transport callback tolerates that.
        self._drop_view(tolerate_export=True)
        return self._w - self._r

    def _drop_view(self, *, tolerate_export: bool = False) -> None:
        """Release the outstanding write window so the buffer can be resized.

        ``tolerate_export`` is for the one call site where the transport can
        legitimately still hold an export: uvloop's buffered read path calls
        ``buffer_updated()`` while its Py_buffer export on the window is still
        acquired (uvloop releases the export in its own finally, immediately
        after our callback returns), so ``release()`` there transiently raises
        ``memoryview has 1 exported buffer`` on the cleartext path (TLS goes
        through SSLProtocol and never hits this).  Dropping the reference is
        enough there — the memoryview is deallocated as soon as uvloop
        releases the export, which it does before the next ``get_buffer``.

        Every other call site (``get_buffer``, ``compact``, ``_make_room``)
        runs after that release, so a BufferError there is a genuine export
        leak from our own code (e.g. a body ``memoryview`` outliving its
        request) and must propagate loudly instead of being masked.  On that
        strict path the reference is deliberately left set (the ``self._view
        = None`` sits after the ``except``): as long as the leak persists,
        every subsequent call fails the same way, so the connection is
        effectively fatal at the first sign of a leak rather than limping on
        to an unrelated crash later.  A body leak can also surface one step
        further on — as the bytearray mutation itself raising
        ``Existing exports of data`` at ``compact``/``_make_room`` — which is
        the same loud failure, from the resize site.
        """
        if self._view is not None:
            try:
                self._view.release()
            except BufferError:
                if not tolerate_export:
                    raise
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

        Two ways to make that copy, and which one wins depends on size — so
        the size decides, the same shape as the send path's join-vs-vectored
        gate.  Slicing the `bytearray` allocates an intermediate and copies
        twice; a `memoryview` slice copies once but pays for building and
        releasing the view.  Measured on this tree (min of 7, µs/call):

        | n | bytearray slice | memoryview | ratio |
        |---|---|---|---|
        | 300 | 0.102 | 0.180 | 1.77× |
        | 4 KiB | 0.197 | 0.242 | 1.23× |
        | 8 KiB | 0.270 | 0.289 | 1.07× |
        | 16 KiB | 0.559 | 0.413 | 0.74× |
        | 1 MiB | 1052 | 16.4 | 0.02× |

        The crossover sits between 8 and 16 KiB.  Below it the view setup
        dominates and the double copy is cheaper — and *every request* takes
        its head through here, so that is the hot path.  Above it the second
        copy dominates and doubles peak memory besides (2.0 → 1.0 MiB on a
        1 MiB take), which matters because a body read asks for whatever the
        peer declared.

        Above the threshold the view is released explicitly rather than left
        to refcounting: a `bytearray` with a live export raises `BufferError`
        on resize, and the next `get_buffer` may resize.  Tying that to when a
        temporary happens to be collected is how it becomes a load-dependent
        crash.
        """
        r = self._r
        if n < _VIEW_COPY_THRESHOLD:
            out = bytes(self._buf[r:r + n])
        else:
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

        Compacting to empty is the one moment a message is provably gone, so it
        raises :attr:`drained_boundary` for the reader's release policy.  The
        flag is a report, not a decision: this object does not know whether a
        connection that has just finished a large message is about to serve
        another one.
        """
        self._drop_view()
        r = self._r
        if r == 0:
            if self._w == 0:
                self.drained_boundary = True
            return
        if self._w == r:
            self._r = self._w = self._scanned = 0
        else:
            del self._buf[:r]
            self._buf.extend(bytes(r))       # keep the allocation, not the data
            self._w -= r
            self._scanned = max(0, self._scanned - r)
            self._r = 0
        if self._w == 0:
            self.drained_boundary = True

    def release_to_floor(self) -> bool:
        """Hand a grown allocation back.  ``True`` when it was given up.

        A single large upload must not leave every connection that served one
        holding its peak allocation for the rest of its keep-alive life — that
        is the idle-memory floor the read window is also sized against.  *When*
        to do that is hysteretic and belongs to the reader, which is the only
        object that knows what the connection has been asked for; this is the
        mechanism it calls.

        Refuses while bytes are resident, and that is not a policy check but
        the one invariant the container owes its caller: the boundary flag is
        raised inside :meth:`compact`, including the compaction
        :meth:`_make_room` does on the *arrival* path, where a delivery lands
        immediately afterwards.  Reallocating there would discard bytes the
        transport has already handed over.
        """
        if not self.grown or self._w != self._r:
            return False
        self._drop_view()
        self._buf = bytearray(_INITIAL)
        self._r = self._w = self._scanned = 0
        self.grown = False
        return True

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
        self.grown = len(self._buf) > _INITIAL
