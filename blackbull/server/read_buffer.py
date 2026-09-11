"""The single owned buffer for the H/1.1 inbound path.

One `bytearray` per connection, written **directly by the kernel** through
:meth:`ReadBuffer.get_buffer` and read by cursor.  Every inbound byte is
materialised once: the head is sliced out for the parser, the body is handed
out as a `memoryview`, and a keep-alive peer's next request is simply the bytes
that were already sitting between the cursors.

Deliberately free of HTTP semantics.  It reports an over-budget head with
:data:`LIMIT_EXCEEDED` rather than raising, because the 431 belongs to the
actor; it distinguishes "EOF with nothing" from "EOF mid-head" only by leaving
:attr:`available` intact, because deciding between a silent close and a 400 is
also the actor's job.

Free of *receive* policy for the same reason: it grows on demand, reports a
drained message boundary, tracks the message's peak resident bytes, and offers
:meth:`release_to_floor`, but takes none of those decisions.  The Internals
page says which object does.

Not thread-safe and not concurrency-safe — one connection, one buffer, one
actor loop, per the actor model.
"""
from __future__ import annotations

__all__ = ('ReadBuffer',)

#: Initial allocation: a typical head plus a small body without a resize,
#: without holding a page per idle keep-alive peer.
_INITIAL = 8192

#: Smallest write window offered to the transport — a **memory floor decision,
#: not a throughput one**.  Whatever is offered here is allocated for the life
#: of every connection, idle ones included, so the 64 KiB a `recv` could use
#: would cost ~640 MB across 10k idle peers.  A large body simply takes more
#: `recv` calls.
_MIN_READ = 4096

#: Compact once the consumed prefix is at least this large *and* at least half
#: the buffer.  The first stops a memmove of a few bytes on every request; the
#: second stops a large resident body being shuffled while it is consumed.
_COMPACT_MIN = 4096

#: Size at or above which :meth:`ReadBuffer.take` copies through a memoryview
#: instead of a `bytearray` slice — the measured crossover, tabulated on
#: ``take``.  Deliberately not configurable: a property of the interpreter's
#: copy costs, not of a deployment.
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

    #: The size :meth:`release_to_floor` returns to.  Public because the
    #: reader's release policy compares a message's peak against it.
    FLOOR = _INITIAL

    def __init__(self) -> None:
        self._buf = bytearray(_INITIAL)
        self._r = 0          # read cursor: first unconsumed byte
        self._w = 0          # write cursor: first free byte
        self._scanned = 0    # absolute offset the head scan has cleared
        self._eof = False
        self._examined = 0   # cumulative bytes the scan has looked at
        #: Allocation is above the floor.  A flag rather than a
        #: ``capacity > FLOOR`` comparison: read on every arrival and every
        #: consuming read, written only where the buffer resizes.
        self.grown = False
        #: A compaction left the buffer empty.  Raised here and cleared by
        #: :meth:`consume_boundary`; this object never reads it.
        self.drained_boundary = False
        #: Peak resident bytes since the last boundary — accounting for the
        #: reader's release hysteresis, kept here because the write path
        #: already holds the cursors.
        self.peak_avail = 0
        # The write window last handed to the transport, held so it can be
        # released before any resize: a bytearray with a live memoryview raises
        # BufferError on grow, so leaving that to the caller's refcount makes it
        # a load-dependent crash rather than a contract.
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
        intended: a scan restarting from the front on every arrival would make
        this quadratic in the number of segments, a peer-chosen CPU cost.
        Overlap of up to three bytes per resumption is the straddled-terminator
        back-off, not a restart.
        """
        return self._examined

    def feed_eof(self) -> None:
        self._eof = True

    # -- BufferedProtocol surface -----------------------------------------

    def get_buffer(self, sizehint: int, *, want: int = 0) -> memoryview:
        """Space for the transport to read into.

        The returned buffer is never empty — an empty one stalls the connection
        permanently — even though asyncio passes ``sizehint = -1`` when it has
        no preference.

        *sizehint* is advisory and is never honoured; the Internals page says
        why.  Growth follows arriving bytes and *want*, the caller's pending
        read size: a parked read of *n* gets a span of up to
        ``min(n, high-water)`` so one recv feeds most of it, and ``want = 0``
        stays at the floor.
        """
        self._drop_view()
        target = want if want > _MIN_READ else _MIN_READ
        if len(self._buf) - self._w < target:
            self._make_room(target)
        # All the free space, not just the target: the allocation is already
        # paid for, so a wider window saves `recv` calls on a large body.
        self._view = memoryview(self._buf)[self._w:]
        return self._view

    def buffer_updated(self, nbytes: int) -> int:
        """Declare how much of the last :meth:`get_buffer` was written.

        Returns the new resident count, and updates ``peak_avail`` on the
        way — but only for a grown buffer, since a floor-sized one can never
        reach the release decision that reads it.
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

        ``tolerate_export`` is for the one caller that can legitimately still
        hold an export: uvloop's buffered read path calls ``buffer_updated()``
        inside its own Py_buffer export and releases it immediately after we
        return, so ``release()`` there raises ``memoryview has 1 exported
        buffer`` on the cleartext path (TLS goes through SSLProtocol and never
        hits this).  Dropping the reference is enough.

        Everywhere else a BufferError is a genuine export leak of ours — a body
        ``memoryview`` outliving its request — and must propagate rather than be
        masked.  On that path the reference is deliberately left set, so every
        later call fails the same way and the connection is fatal at the first
        sign of a leak instead of crashing somewhere unrelated later.
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
        # Clear up to the terminator's *start*, not past it: advancing past it
        # would make a repeat call search from beyond the match and report
        # "not found" for a head sitting right there.
        self._scanned = idx
        end = idx + 4 - self._r
        if limit > 0 and end > limit:
            return self.LIMIT_EXCEEDED
        return end

    def find(self, sep: bytes, start: int = 0) -> int:
        """Offset of *sep* within the resident bytes, or ``-1``.

        Scans from the read cursor every call, unlike :meth:`find_head_end`:
        its callers change the search target between calls, so a carried scan
        offset would be wrong rather than merely wasteful.  *start* is a
        relative offset for a caller resuming its own scan on a separator that
        is still pending.
        """
        idx = self._buf.find(sep, self._r + start, self._w)
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
        byte ever received on it.  Compacting to empty is the one moment a
        message is provably gone, so it raises :attr:`drained_boundary`.
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

    def consume_boundary(self) -> None:
        """Take the raised boundary, and start the next message's accounting.

        The reader polls :attr:`drained_boundary` and calls this when it is
        set.  Clearing the flag and the peak has to happen here rather than at
        the call site: a consumer resetting them itself makes an edge-triggered
        signal whose edge goes to whichever consumer reaches it first.
        """
        self.drained_boundary = False
        self.peak_avail = 0

    def release_to_floor(self) -> bool:
        """Hand a grown allocation back.  ``True`` when it was given up.

        The mechanism behind the reader's hysteretic release policy: a single
        large upload must not leave every connection that served one holding
        its peak allocation for the rest of its keep-alive life.

        Refuses while bytes are resident.  That is the one invariant this
        container owes its caller, not a policy check — :attr:`drained_boundary`
        is also raised on the *arrival* path, where a delivery lands
        immediately afterwards and reallocating would discard bytes the
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
