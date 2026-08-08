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

    def test_a_large_body_does_not_leave_a_permanent_allocation(self):
        """Peak allocation is released once the message is consumed.

        Otherwise every connection that ever served one big upload holds its
        peak for the rest of its keep-alive life — the same idle-memory floor
        the read window is sized against, arrived at from the other side.
        """
        rb = ReadBuffer()
        _feed(rb, b'H\r\n\r\n' + b'z' * 200_000)
        rb.take(rb.find_head_end())
        assert rb.capacity > 100_000          # grew to hold the body
        rb.consume(200_000)
        rb.compact()
        assert rb.capacity <= 8192, f'stayed at {rb.capacity} after the body'

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
    def test_get_buffer_returns_at_least_the_hint(self):
        rb = ReadBuffer()
        assert len(rb.get_buffer(64)) >= 64
        assert len(rb.get_buffer(1 << 20)) >= (1 << 20)

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
