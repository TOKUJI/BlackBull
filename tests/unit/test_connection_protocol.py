"""``ConnectionProtocol`` / ``BufferReader`` — the transport front end over one buffer.

The protocol replaces ``asyncio.StreamReader`` on the H/1.1 inbound path: the
kernel writes into the connection's own buffer via ``get_buffer``, and the
actor's coroutine parks on a future that ``buffer_updated`` resolves.  That is
the same shape sanic uses, and it keeps the actor invariant — one coroutine
owning the connection's state — while removing the second buffer.

Assertions here are about the *seams*: does a parked reader wake, does EOF
unblock rather than hang, does a reader that already has bytes avoid
suspending at all, and does an upgrade hand its surplus over intact.
"""
import asyncio
import time

import pytest

from blackbull.server.connection_protocol import ConnectionProtocol
from blackbull.server.read_buffer import ReadBuffer
from blackbull.server.recipient import ReadLimitExceeded


class _FakeTransport:
    def __init__(self):
        self.closed = False
        self.paused = False
        self.written = bytearray()
        self.eofs = 0

    def close(self):
        self.closed = True

    def write(self, data):
        self.written += data

    def writelines(self, parts):
        for part in parts:
            self.written += part

    def can_write_eof(self):
        return True

    def write_eof(self):
        self.eofs += 1

    def pause_reading(self):
        self.paused = True

    def resume_reading(self):
        self.paused = False

    def get_extra_info(self, name, default=None):
        return {'peername': ('127.0.0.1', 1234),
                'sockname': ('127.0.0.1', 8000)}.get(name, default)

    def is_closing(self):
        return self.closed


def _deliver(proto: ConnectionProtocol, data: bytes) -> int:
    """Feed *data* the way the selector transport does.

    A real transport writes at most ``len(get_buffer(...))`` per read and stops
    calling ``get_buffer`` once ``pause_reading`` has been honoured, so this
    loops in windows and stops when paused.  Returns bytes delivered.
    """
    sent = 0
    while sent < len(data):
        if proto.transport is not None and proto.transport.paused:
            break
        view = proto.get_buffer(-1)
        n = min(len(view), len(data) - sent)
        view[:n] = data[sent:sent + n]
        proto.buffer_updated(n)
        sent += n
    return sent


pytestmark = pytest.mark.asyncio


@pytest.fixture
def wired():
    proto = ConnectionProtocol()
    transport = _FakeTransport()
    proto.connection_made(transport)
    return proto, transport


class TestReaderWakes:
    async def test_read_parks_until_data_arrives(self, wired):
        proto, _ = wired
        reader = proto.reader

        got = []

        async def consumer():
            got.append(await reader.read(5))

        task = asyncio.create_task(consumer())
        await asyncio.sleep(0)                 # let it park
        assert not task.done(), 'reader returned before any data arrived'
        _deliver(proto, b'hello')
        await asyncio.wait_for(task, timeout=1)
        assert got == [b'hello']

    async def test_read_with_bytes_already_resident_does_not_suspend(self, wired):
        """The keep-alive win: a pipelined head is already in the buffer, so
        the read completes without a loop turn.

        Asserted by draining the coroutine with ``send`` — if it suspends, it
        yields a future instead of raising StopIteration.
        """
        proto, _ = wired
        _deliver(proto, b'GET / HTTP/1.1\r\n\r\n')
        coro = proto.reader.read(4)
        try:
            coro.send(None)
        except StopIteration as done:
            assert done.value == b'GET '
        else:
            coro.close()
            pytest.fail('read suspended even though bytes were resident')

    async def test_eof_unblocks_a_parked_reader(self, wired):
        proto, _ = wired

        async def consumer():
            return await proto.reader.read(10)

        task = asyncio.create_task(consumer())
        await asyncio.sleep(0)
        proto.eof_received()
        assert await asyncio.wait_for(task, timeout=1) == b''

    async def test_connection_lost_unblocks_with_the_error(self, wired):
        proto, _ = wired

        async def consumer():
            return await proto.reader.read(10)

        task = asyncio.create_task(consumer())
        await asyncio.sleep(0)
        proto.connection_lost(ConnectionResetError('peer gone'))
        with pytest.raises(ConnectionResetError):
            await asyncio.wait_for(task, timeout=1)

    async def test_readexactly_spanning_two_arrivals(self, wired):
        proto, _ = wired

        async def consumer():
            return await proto.reader.readexactly(8)

        task = asyncio.create_task(consumer())
        await asyncio.sleep(0)
        _deliver(proto, b'abcd')
        await asyncio.sleep(0)
        assert not task.done()
        _deliver(proto, b'efgh')
        assert await asyncio.wait_for(task, timeout=1) == b'abcdefgh'

    async def test_readuntil_spanning_two_arrivals(self, wired):
        proto, _ = wired

        async def consumer():
            return await proto.reader.readuntil(b'\r\n')

        task = asyncio.create_task(consumer())
        await asyncio.sleep(0)
        _deliver(proto, b'partial')
        await asyncio.sleep(0)
        assert not task.done()
        _deliver(proto, b' line\r\nrest')
        assert await asyncio.wait_for(task, timeout=1) == b'partial line\r\n'
        assert proto.reader.buffered_len() == 4        # 'rest' stays put

    async def test_readuntil_limit_fails_while_line_is_accumulating(self, wired):
        proto, _ = wired

        async def consumer():
            return await proto.reader.readuntil(b'\n', limit=8)

        task = asyncio.create_task(consumer())
        await asyncio.sleep(0)
        _deliver(proto, b'12345678')
        await asyncio.sleep(0)
        assert not task.done()
        _deliver(proto, b'9')
        with pytest.raises(ReadLimitExceeded) as caught:
            await asyncio.wait_for(task, timeout=1)
        assert caught.value.seen == b'123456789'
        assert proto.reader.buffered_len() == 9

    async def test_readuntil_limit_includes_separator(self, wired):
        proto, _ = wired
        _deliver(proto, b'1234567\nrest')

        assert await proto.reader.readuntil(b'\n', limit=8) == b'1234567\n'
        assert proto.reader.buffered_len() == 4

    async def test_readuntil_limit_handles_separator_split_across_arrivals(self, wired):
        proto, _ = wired

        async def consumer():
            return await proto.reader.readuntil(b'\r\n', limit=8)

        task = asyncio.create_task(consumer())
        await asyncio.sleep(0)
        _deliver(proto, b'123456')
        await asyncio.sleep(0)
        _deliver(proto, b'\r')
        await asyncio.sleep(0)
        assert not task.done()
        _deliver(proto, b'\nrest')

        assert await asyncio.wait_for(task, timeout=1) == b'123456\r\n'
        assert proto.reader.buffered_len() == 4

    async def test_readuntil_resumes_scan_in_linear_work(self, wired, monkeypatch):
        proto, _ = wired
        examined = 0
        original = ReadBuffer.find

        def counting_find(buffer, sep: bytes, start: int = 0) -> int:
            nonlocal examined
            examined += buffer.available - start
            return original(buffer, sep, start)

        monkeypatch.setattr(ReadBuffer, 'find', counting_find)
        task = asyncio.create_task(proto.reader.readuntil(b'\n', limit=32))
        await asyncio.sleep(0)
        for byte in b'x' * 33:
            _deliver(proto, bytes((byte,)))
            await asyncio.sleep(0)

        with pytest.raises(ReadLimitExceeded):
            await asyncio.wait_for(task, timeout=1)
        assert examined <= 33


class TestHeadScan:
    async def test_head_is_taken_without_per_line_reads(self, wired):
        proto, _ = wired
        _deliver(proto, b'GET / HTTP/1.1\r\nhost: x\r\naccept: */*\r\n\r\nBODY')
        head = await proto.reader.read_head(limit=8192)
        assert head == b'GET / HTTP/1.1\r\nhost: x\r\naccept: */*\r\n\r\n'
        assert proto.reader.buffered_len() == 4

    async def test_head_over_budget_reports_the_bytes_it_saw(self, wired):
        """The reader reports the breach; the protocol decides the status.

        Which means the evidence has to travel with the exception — the actor
        answers 400 for an over-budget blob with no line terminator in it and
        431 for one that is merely a long head, and it must reach that verdict
        the same way whichever reader it is sitting on.
        """
        from blackbull.server.recipient import ReadLimitExceeded
        proto, _ = wired
        _deliver(proto, b'x' * 5000)
        with pytest.raises(ReadLimitExceeded) as caught:
            await proto.reader.read_head(limit=4096)
        assert caught.value.seen.startswith(b'xxx')
        assert b'\r\n' not in caught.value.seen
        # Not consumed: the lingering close still has bytes to discard, which
        # is what keeps the response from being RST away.
        assert proto.reader.buffered_len() == 5000

    async def test_head_read_waits_for_the_terminator(self, wired):
        proto, _ = wired

        async def consumer():
            return await proto.reader.read_head(limit=8192)

        task = asyncio.create_task(consumer())
        await asyncio.sleep(0)
        _deliver(proto, b'GET / HTTP/1.1\r\n')
        await asyncio.sleep(0)
        assert not task.done()
        _deliver(proto, b'\r\n')
        assert await asyncio.wait_for(task, timeout=1) == b'GET / HTTP/1.1\r\n\r\n'

    async def test_eof_mid_head_is_distinguishable_from_idle_eof(self, wired):
        """``run()`` answers 400 for a partial head and closes silently for an
        idle one, so the two must not collapse into one signal."""
        from blackbull.server.recipient import IncompleteReadError
        proto, _ = wired
        _deliver(proto, b'GET / HTTP/1.1\r\n')
        proto.eof_received()
        with pytest.raises(IncompleteReadError) as caught:
            await proto.reader.read_head(limit=8192)
        assert caught.value.partial == b'GET / HTTP/1.1\r\n'

    async def test_idle_eof_is_an_empty_head_not_an_error(self, wired):
        """A peer that opens a connection and closes it without sending
        anything is not a truncated request — it gets no response at all."""
        proto, _ = wired
        proto.eof_received()
        assert await proto.reader.read_head(limit=8192) == b''


class TestUpgradeHandoff:
    async def test_surplus_survives_being_handed_to_a_successor(self, wired):
        """WebSocket and h2c hand the connection on mid-stream.

        Under the streams design the successor got a reader plus a separately
        carried prefix; here the surplus is already resident, so the hand-off
        is the same object and there is nothing to carry.
        """
        proto, _ = wired
        _deliver(proto, b'GET /ws HTTP/1.1\r\n\r\n\x81\x05hello')
        await proto.reader.read_head(limit=8192)
        assert proto.reader.buffered_len() == 7
        assert proto.reader.peek(7) == b'\x81\x05hello'
        # The successor reads frames off the very same reader.
        assert await proto.reader.read(7) == b'\x81\x05hello'


class TestFlowControl:
    async def test_reading_is_paused_when_the_buffer_outgrows_the_watermark(
            self, wired):
        """An unread buffer must stop the transport, or a fast peer with a slow
        handler grows it without bound — the memory side of backpressure."""
        proto, transport = wired
        sent = _deliver(proto, b'z' * (1024 * 1024))
        assert transport.paused, 'transport was not paused past the watermark'
        assert sent < 1024 * 1024, 'delivery was never throttled'
        assert proto.reader.buffered_len() >= 128 * 1024

    async def test_reading_resumes_once_the_buffer_drains(self, wired):
        proto, transport = wired
        sent = _deliver(proto, b'z' * (1024 * 1024))
        assert transport.paused
        # Drain to below the low-water mark; the gap between the marks is what
        # stops a pause/resume flap on alternate reads.
        await proto.reader.readexactly(sent - 1024)
        assert not transport.paused, 'transport stayed paused after draining'


class TestReaderSurface:
    async def test_at_eof_only_after_the_buffer_is_drained(self, wired):
        proto, _ = wired
        _deliver(proto, b'tail')
        proto.eof_received()
        assert not proto.reader.at_eof(), 'EOF reported with bytes still unread'
        assert await proto.reader.read(4) == b'tail'
        assert proto.reader.at_eof()

    async def test_has_buffered_and_peek_do_not_consume(self, wired):
        proto, _ = wired
        _deliver(proto, b'abcdef')
        assert proto.reader.has_buffered()
        assert proto.reader.peek(3) == b'abc'
        assert proto.reader.buffered_len() == 6
        assert await proto.reader.read(6) == b'abcdef'


class TestWriteSide:
    async def test_drain_returns_without_awaiting_when_not_paused(self, wired):
        """One loop turn per send is exactly the cost being removed on the read
        side; the write side must not reintroduce it."""
        proto, _ = wired
        coro = proto.drain()
        try:
            coro.send(None)
        except StopIteration:
            pass
        else:
            coro.close()
            pytest.fail('drain suspended with the transport unpaused')

    async def test_drain_blocks_while_paused_and_resumes(self, wired):
        proto, _ = wired
        proto.pause_writing()
        task = asyncio.create_task(proto.drain())
        await asyncio.sleep(0)
        assert not task.done()
        proto.resume_writing()
        await asyncio.wait_for(task, timeout=1)

    async def test_connection_lost_unblocks_a_parked_drain(self, wired):
        """A peer that vanishes mid-response must not strand the sender.

        Without this the write path waits for a resume_writing that can never
        arrive, holding the connection for its whole lifetime.
        """
        proto, _ = wired
        proto.pause_writing()
        task = asyncio.create_task(proto.drain())
        await asyncio.sleep(0)
        proto.connection_lost(ConnectionResetError('peer gone'))
        with pytest.raises(ConnectionResetError):
            await asyncio.wait_for(task, timeout=1)


class TestLingerClose:
    """Whether a connection lingers is decided by the connection, once.

    The refusal path calls ``linger_close`` twice — the refusal itself, then the
    serving task's teardown.  On a socket the second call finds the EOF that
    ``connection_lost`` has already set, so the socket-level pins cannot tell a
    guarded second call from an unguarded one; ``_FakeTransport`` never reports
    loss, which is what makes the guard observable.  Without this pin the next
    reduction pass would delete it as dead code.
    """

    async def test_the_linger_decision_is_taken_once_per_connection(self, wired):
        proto, transport = wired
        proto.write(b'HTTP/1.1 503 Service Unavailable\r\n\r\n')
        window = 0.1
        await proto.linger_close(timeout=window)
        assert transport.eofs == 1, 'the first call opened no window'

        started = time.perf_counter()
        await proto.linger_close(timeout=window)
        elapsed = time.perf_counter() - started

        assert transport.eofs == 1, 'the second call opened a second window'
        assert elapsed < window / 2, (
            f'the second call waited {elapsed:.3f}s: the decision was remade')

    async def test_a_vectored_response_owes_the_window_too(self, wired):
        """The other write entry point arms the same state, not just ``write``."""
        proto, transport = wired
        proto.writelines([b'HTTP/1.1 503 Service Unavailable\r\n\r\n', b'x' * 64])
        await proto.linger_close(timeout=0.05)
        assert transport.eofs == 1, 'writelines left the window unarmed'

    async def test_an_empty_write_owes_nothing(self, wired):
        proto, transport = wired
        proto.write(b'')
        proto.writelines([])
        await proto.linger_close(timeout=0.05)
        assert transport.eofs == 0, 'a write of no bytes armed the window'
