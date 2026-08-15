"""``ReadBuffer`` — the single owned buffer for the H/1.1 inbound path.

The whole point of this object is that inbound bytes are materialised **once**.
Every assertion here is about that invariant in one of its forms: the scan does
not re-examine what it has already examined, surplus is never copied or handed
back, and a body is a view rather than a slice until someone asks for bytes.

These are unit tests with no I/O — the buffer is fed the way the protocol's
``buffer_updated`` will feed it.
"""
import pytest

from blackbull.server.read_buffer import ReadBuffer


def _feed(rb: ReadBuffer, data: bytes) -> None:
    """Write *data* the way ``BufferedProtocol`` does: into the buffer the
    reader handed out, then declare how much landed."""
    view = rb.get_buffer(len(data))
    view[:len(data)] = data
    rb.buffer_updated(len(data))


def _feed_chunked(rb: ReadBuffer, data: bytes) -> None:
    """Write *data* the way a real transport does — in window-sized chunks.

    ``get_buffer``'s window is the buffer's free span (the sizehint is
    advisory — F5), so a message larger than the current allocation fills it
    in pieces and the buffer grows only as bytes actually arrive.
    """
    pos = 0
    while pos < len(data):
        view = rb.get_buffer(len(data) - pos)
        n = min(len(view), len(data) - pos)
        view[:n] = data[pos:pos + n]
        rb.buffer_updated(n)
        pos += n


class TestSingleMaterialisation:
    def test_head_split_across_arrivals_is_found(self):
        rb = ReadBuffer()
        _feed(rb, b'GET / HTTP/1.1\r\n')
        assert rb.find_head_end() == -1
        _feed(rb, b'host: localhost\r\n')
        assert rb.find_head_end() == -1
        _feed(rb, b'\r\n')
        assert rb.find_head_end() == len(
            b'GET / HTTP/1.1\r\nhost: localhost\r\n\r\n')

    def test_head_scan_is_linear_under_one_byte_dribble(self):
        """The scan must not restart from the front on every arrival.

        A peer that sends a 2 KiB head one byte per segment is the worst case:
        re-scanning the whole buffer each time is O(n²) — ~2 million byte
        comparisons here — and the peer chooses n.  Resuming from the last
        cleared offset keeps it linear, with only the 3-byte straddle back-off
        as overlap.
        """
        head = b'GET / HTTP/1.1\r\n' + b'x-pad: ' + b'a' * 2000 + b'\r\n\r\n'
        rb = ReadBuffer()
        for i in range(len(head)):
            _feed(rb, head[i:i + 1])
            rb.find_head_end()
        assert rb.find_head_end() == len(head)     # and idempotent
        # One-byte segments are the worst case for the back-off: every arrival
        # re-examines the 3-byte straddle window plus its 1 new byte, so ~4x
        # is the expected linear constant.  The bound is set to separate
        # linear from quadratic (~500x here), not to pin the constant.
        quadratic = len(head) ** 2 // 2
        assert rb.examined_bytes < 6 * len(head), (
            f'scan examined {rb.examined_bytes} bytes for a {len(head)}-byte '
            f'head — linear is ~{4 * len(head)}, quadratic ~{quadratic}')

    def test_delimiter_straddling_an_arrival_boundary_is_found(self):
        # The terminator split down the middle: '\r\n\r' then '\n'.  A scan
        # that resumed at exactly the write cursor would step over it.
        rb = ReadBuffer()
        _feed(rb, b'GET / HTTP/1.1\r\nhost: x\r\n\r')
        assert rb.find_head_end() == -1
        _feed(rb, b'\n')
        assert rb.find_head_end() == len(b'GET / HTTP/1.1\r\nhost: x\r\n\r\n')

    def test_take_head_returns_bytes_and_advances_the_cursor(self):
        rb = ReadBuffer()
        _feed(rb, b'GET / HTTP/1.1\r\n\r\nBODY')
        end = rb.find_head_end()
        head = rb.take(end)
        assert head == b'GET / HTTP/1.1\r\n\r\n'
        assert rb.available == 4          # BODY still resident, uncopied


class TestSurplusIsResident:
    def test_pipelined_second_request_needs_no_pushback(self):
        """Surplus is just 'bytes between the cursors'.

        The layered reader this replaces had to ``unread`` a prefix back onto
        itself; here the next request is already in place, which is what makes
        the whole pushback class of bug unrepresentable.
        """
        rb = ReadBuffer()
        _feed(rb, b'GET /a HTTP/1.1\r\n\r\nGET /b HTTP/1.1\r\n\r\n')
        first = rb.take(rb.find_head_end())
        assert first == b'GET /a HTTP/1.1\r\n\r\n'
        second_end = rb.find_head_end()
        assert rb.take(second_end) == b'GET /b HTTP/1.1\r\n\r\n'
        assert rb.available == 0

    def test_scan_state_resets_per_message_but_keeps_surplus(self):
        # After taking a head the scan must restart for the next message,
        # otherwise the second head is searched from an offset past its own
        # terminator and never found.
        rb = ReadBuffer()
        _feed(rb, b'GET /a HTTP/1.1\r\nhost: x\r\n\r\n')
        rb.take(rb.find_head_end())
        assert rb.find_head_end() == -1        # nothing of message 2 yet
        _feed(rb, b'GET /b HTTP/1.1\r\n\r\n')
        assert rb.take(rb.find_head_end()) == b'GET /b HTTP/1.1\r\n\r\n'


class TestBodyAsView:
    def test_view_does_not_copy_and_tracks_the_buffer(self):
        rb = ReadBuffer()
        _feed(rb, b'HEAD\r\n\r\n' + b'x' * 100)
        rb.take(rb.find_head_end())
        v = rb.view(100)
        assert isinstance(v, memoryview)
        assert len(v) == 100
        assert bytes(v) == b'x' * 100

    def test_take_after_view_advances_past_the_body(self):
        rb = ReadBuffer()
        _feed(rb, b'H\r\n\r\n' + b'y' * 10 + b'NEXT')
        rb.take(rb.find_head_end())
        assert bytes(rb.view(10)) == b'y' * 10
        rb.consume(10)
        assert rb.available == 4
        assert bytes(rb.view(4)) == b'NEXT'


class TestCompaction:
    def test_buffer_does_not_grow_without_bound_across_requests(self):
        """Many sequential requests must not grow the allocation.

        Without compaction the read cursor walks forward forever and the
        bytearray grows to the total bytes ever received on the connection —
        a keep-alive connection would leak for its whole lifetime.
        """
        rb = ReadBuffer()
        for _ in range(200):
            _feed(rb, b'GET / HTTP/1.1\r\n\r\n')
            rb.take(rb.find_head_end())
        assert rb.available == 0
        assert rb.capacity <= 8192, f'buffer grew to {rb.capacity}'

    def test_compacting_to_empty_reports_the_message_boundary(self):
        """The buffer's whole share of the release policy: it says when a
        message is provably gone and offers the allocation back.

        Whether to take that offer is hysteretic and belongs to
        ``BufferReader``, which is the only object that knows what the
        connection has been asked for — asserted in
        ``test_receive_decisions.py``.
        """
        rb = ReadBuffer()
        _feed(rb, b'H\r\n\r\n')
        _feed_chunked(rb, b'z' * 200_000)
        rb.take(rb.find_head_end())
        assert rb.capacity > 100_000          # grew to hold the body
        assert rb.grown
        rb.consume(200_000)
        rb.compact()

        assert rb.drained_boundary, 'a drained compaction is a message boundary'
        assert rb.capacity > 100_000, 'the buffer released on its own judgement'
        assert rb.release_to_floor() is True
        assert rb.capacity == ReadBuffer.FLOOR
        assert not rb.grown

    def test_the_buffer_never_shrinks_on_its_own_judgement(self):
        """Repeated boundaries with nobody deciding must leave the allocation
        alone — a buffer that shrinks here kept a policy it handed over."""
        rb = ReadBuffer()
        _feed(rb, b'H\r\n\r\n')
        _feed_chunked(rb, b'z' * 200_000)
        rb.take(rb.find_head_end())
        grown = rb.capacity
        rb.consume(200_000)
        for _ in range(8):
            rb.compact()
        assert rb.capacity == grown, 'the buffer released its own allocation'
        assert rb.drained_boundary, 'and stopped reporting the boundary besides'

    def test_the_release_never_discards_resident_bytes(self):
        """The one precondition the mechanism keeps for itself.

        The boundary flag is raised inside ``compact()`` — including the
        compaction ``_make_room`` does on the *arrival* path, where bytes land
        immediately afterwards.  A release firing there would throw away a
        delivery the transport has already handed over.
        """
        rb = ReadBuffer()
        _feed(rb, b'H\r\n\r\n')
        _feed_chunked(rb, b'z' * 200_000)
        rb.take(rb.find_head_end())
        rb.consume(200_000)
        rb.compact()
        _feed(rb, b'keep')

        assert rb.release_to_floor() is False, (
            'released an allocation that still held a delivery')
        assert bytes(rb.view(4)) == b'keep'

    def test_compaction_preserves_unconsumed_surplus(self):
        rb = ReadBuffer()
        for _ in range(200):
            _feed(rb, b'GET / HTTP/1.1\r\n\r\n')
            rb.take(rb.find_head_end())
        _feed(rb, b'PARTIAL HEAD\r\n')
        rb.compact()
        assert bytes(rb.view(rb.available)) == b'PARTIAL HEAD\r\n'


class TestLimitsAndEof:
    def test_head_over_the_budget_is_reported_not_raised(self):
        # The buffer owns no HTTP semantics: it reports the overrun and the
        # actor owns the 431 that follows.
        rb = ReadBuffer()
        _feed(rb, b'x' * 5000)
        assert rb.find_head_end(limit=4096) == ReadBuffer.LIMIT_EXCEEDED

    def test_eof_with_partial_head_is_distinguishable_from_idle_eof(self):
        # run() answers 400 for one and closes silently for the other, so the
        # buffer has to keep them apart.
        idle = ReadBuffer()
        idle.feed_eof()
        assert idle.at_eof and idle.available == 0

        partial = ReadBuffer()
        _feed(partial, b'GET / HTTP/1.1\r\n')
        partial.feed_eof()
        assert partial.at_eof and partial.available == 16

    def test_scan_resumes_correctly_after_eof_without_terminator(self):
        rb = ReadBuffer()
        _feed(rb, b'GET / HTTP/1.1\r\n')
        rb.feed_eof()
        assert rb.find_head_end() == -1


class TestResizeSafety:
    def test_buffer_grows_while_a_write_window_is_still_referenced(self):
        """A `bytearray` cannot be resized while a `memoryview` of it lives.

        asyncio holds the window returned by ``get_buffer`` across its
        ``recv_into``, so a grow that happens while the caller still has a
        reference raises ``BufferError``.  Whether the caller has dropped it
        depends on refcount timing, which would make this a load-dependent
        crash rather than a contract — the buffer releases its own window
        instead of relying on that.
        """
        rb = ReadBuffer()
        held = []
        for _ in range(400):
            view = rb.get_buffer(-1)
            held.append(view)                 # never released by the caller
            n = min(len(view), 1024)
            view[:n] = b'q' * n
            rb.buffer_updated(n)
        assert rb.available == 400 * 1024

    def test_growth_is_geometric_not_incremental(self):
        """Growing by exactly what was asked reallocates on nearly every read.

        For a large body that is O(n) memmoves over the message; doubling
        makes it O(log n).
        """
        rb = ReadBuffer()
        caps = set()
        for _ in range(400):
            view = rb.get_buffer(-1)
            n = min(len(view), 1024)
            view[:n] = b'q' * n
            rb.buffer_updated(n)
            caps.add(rb.capacity)
        assert len(caps) < 16, f'{len(caps)} reallocations for 400 KiB'


class TestGetBufferContract:
    def test_get_buffer_returns_usable_space_not_the_raw_hint(self):
        """The sizehint is advisory, not a minimum-window contract.

        uvloop's cleartext path passes libuv's fixed 64 KiB hint on every
        call; honouring it (``want = max(sizehint, _MIN_READ)``) grew the
        buffer to 64 KiB and the reader's release policy gave it back at every message
        boundary — a 64 KiB alloc/free churn per request (the F5 finding).
        The window is the buffer's free span (never below the read floor);
        growth happens only when bytes actually arriving fill the buffer.
        """
        rb = ReadBuffer()
        # A fresh 8 KiB buffer offers its free span regardless of the hint.
        assert len(rb.get_buffer(64)) >= 4096
        big = rb.get_buffer(1 << 20)
        assert 4096 <= len(big) < (1 << 20)    # hint not honoured; no 1 MiB alloc
        # Data actually arriving is what drives growth.
        for _ in range(3):
            view = rb.get_buffer(-1)
            n = min(len(view), 8192)
            view[:n] = b'x' * n
            rb.buffer_updated(n)
        assert rb.capacity > 8192              # grew for the resident data
        assert len(rb.get_buffer(1 << 20)) >= 4096

    def test_zero_hint_still_returns_usable_space(self):
        # asyncio passes sizehint=-1 or 0; returning an empty buffer would
        # stall the transport permanently.
        rb = ReadBuffer()
        assert len(rb.get_buffer(-1)) > 0
        assert len(rb.get_buffer(0)) > 0

    def test_partial_fill_is_honoured(self):
        rb = ReadBuffer()
        view = rb.get_buffer(4096)
        view[:3] = b'abc'
        rb.buffer_updated(3)
        assert rb.available == 3
        assert bytes(rb.view(3)) == b'abc'


class TestDropViewUnderTransportExport:
    class _ExportHeld:
        """A write window whose release() raises exactly as uvloop's still-held
        Py_buffer export does on the cleartext path."""

        def release(self):
            raise BufferError('memoryview has 1 exported buffer')

    def test_buffer_updated_tolerates_transport_export(self):
        """uvloop's buffered read path calls ``buffer_updated`` while its
        Py_buffer export on the get_buffer window is still acquired (the
        export is released in uvloop's own finally, immediately after our
        callback returns), so a ``release()`` there transiently raises
        ``BufferError: memoryview has 1 exported buffer``.  The
        ``buffer_updated`` call site tolerates it and drops the reference;
        the transport releases the export before the next ``get_buffer``."""
        rb = ReadBuffer()
        rb._view = self._ExportHeld()          # transport still owns the export
        rb._drop_view(tolerate_export=True)    # the buffer_updated call site
        assert rb._view is None
        # A fresh window works and is again droppable.
        view = rb.get_buffer(16)
        view[:3] = b'abc'
        rb.buffer_updated(3)
        assert rb._view is None
        assert rb.available == 3

    def test_other_call_sites_are_strict(self):
        """get_buffer / compact / _make_room run after uvloop has released
        the export, so a BufferError there is a genuine leak from our own
        code (e.g. a body memoryview outliving its request) and must
        propagate loudly instead of being masked."""
        rb = ReadBuffer()
        rb._view = self._ExportHeld()
        with pytest.raises(BufferError):
            rb._drop_view()                    # strict: propagate
        assert rb._view is not None            # reference kept on failure

    def test_a_held_body_view_surfaces_as_a_loud_buffer_error(self):
        """A body memoryview still alive across a compact is the genuine
        leak the strict path protects: it surfaces as ``BufferError`` (the
        bytearray cannot resize with a live export), not a silent corruption.

        Note this fires at the mutation site (``del self._buf[:r]``), not at
        ``_drop_view``'s release — a real write-window export can only be
        created by a C-level buffer-protocol consumer like uvloop, which is
        why the strict branch itself is exercised with the ``_ExportHeld``
        stub above."""
        rb = ReadBuffer()
        _feed(rb, b'HEAD\r\n\r\n' + b'x' * 100)
        rb.take(rb.find_head_end())
        v = rb.view(100)                       # the leak: body view kept alive
        try:
            with pytest.raises(BufferError):
                rb.compact()
        finally:
            v.release()
