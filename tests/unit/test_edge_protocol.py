"""Unit tests for _BlackBullProtocol — the asyncio.Protocol subclass
that wires ProtocolBuffer into transport callbacks.

Sprint 30 Tier 1.5 Step 2.  Tests use a fake Transport so the
protocol's behaviour is exercised without spinning up a real
asyncio.Server.

Coverage:
- connection_made: creates buffer + writer + spawns connected_cb task.
- data_received: feeds the buffer.
- eof_received: synchronously closes the transport and feeds EOF to
  the buffer; returns False so asyncio also closes (no half-open).
- connection_lost: closes the buffer (final cleanup) and lets
  FlowControlMixin tidy its state.
- Integration: an actor-style consumer reading from the buffer sees
  data immediately when fed; sees IncompleteReadError when EOF
  arrives via eof_received.
"""
from __future__ import annotations

import asyncio
from typing import Optional

import pytest

from blackbull.server.edge_protocol import _BlackBullProtocol
from blackbull.server.protocol_buffer import ProtocolBuffer
from blackbull.server.recipient import IncompleteReadError


# ---------------------------------------------------------------------------
# Fake transport — minimal asyncio.Transport surface
# ---------------------------------------------------------------------------

class _FakeTransport(asyncio.Transport):
    """A minimal asyncio.Transport for testing protocols in isolation.

    Records calls to ``close``, ``write``, etc. so tests can assert on
    them.  Doesn't actually transport anything.
    """

    def __init__(self, extra: Optional[dict] = None):
        super().__init__(extra=extra or {})
        self.closed: bool = False
        self.writes: list[bytes] = []
        self._protocol: Optional[asyncio.Protocol] = None

    # --- BaseTransport ---

    def close(self) -> None:
        self.closed = True
        # Real asyncio schedules connection_lost; tests can drive it
        # explicitly via _fire_connection_lost.

    def is_closing(self) -> bool:
        return self.closed

    def get_extra_info(self, name, default=None):
        if name == 'sslcontext':
            return None
        return self._extra.get(name, default)

    # --- Transport (write side) ---

    def write(self, data: bytes) -> None:
        self.writes.append(bytes(data))

    def writelines(self, list_of_data) -> None:
        for d in list_of_data:
            self.write(d)

    # Stub methods below — asyncio.Transport requires them, but these
    # tests never exercise the write-buffer / read-pause control surface.
    # Bodies are intentional no-ops; ``...`` (Ellipsis) is recognised by
    # CodeQL as an explicit empty stub.

    def write_eof(self) -> None:
        ...

    def can_write_eof(self) -> bool:
        return True

    def get_write_buffer_size(self) -> int:
        return 0

    def get_write_buffer_limits(self) -> tuple[int, int]:
        return (0, 0)

    def set_write_buffer_limits(self, high=None, low=None) -> None:
        ...

    def pause_reading(self) -> None:
        ...

    def resume_reading(self) -> None:
        ...

    def set_protocol(self, protocol) -> None:
        self._protocol = protocol

    def get_protocol(self):
        return self._protocol

    # --- Test helpers ---

    def _fire_connection_lost(self, exc: Optional[BaseException] = None) -> None:
        """Simulate asyncio firing connection_lost after a transport close."""
        if self._protocol is not None:
            self._protocol.connection_lost(exc)


# ---------------------------------------------------------------------------
# Test fixtures
# ---------------------------------------------------------------------------

class _ConsumerRecorder:
    """Records calls to the connected_cb so tests can assert on them
    and drive the consumer task's read behaviour."""

    def __init__(self):
        self.calls: list[tuple] = []          # (buffer, writer) pairs
        self.read_result: Optional[bytes | BaseException] = None
        self.consumer_done = asyncio.Event()

    async def __call__(self, buffer: ProtocolBuffer,
                       writer: asyncio.StreamWriter) -> None:
        self.calls.append((buffer, writer))
        # Default: read one request line then exit.  Tests can override
        # by setting read_result before invoking.
        try:
            self.read_result = await buffer.read_until(b'\r\n')
        except Exception as exc:
            self.read_result = exc
        finally:
            self.consumer_done.set()


# ---------------------------------------------------------------------------
# connection_made
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_connection_made_creates_buffer_writer_and_task():
    cb = _ConsumerRecorder()
    protocol = _BlackBullProtocol(cb)
    transport = _FakeTransport()
    transport.set_protocol(protocol)

    protocol.connection_made(transport)

    # Wait one loop iteration for the spawned task to start.
    await asyncio.sleep(0)

    assert protocol.buffer is not None
    assert protocol.writer is not None
    assert protocol.task is not None
    assert protocol.transport is transport
    assert len(cb.calls) == 1
    # The callback got our buffer + writer.
    assert cb.calls[0][0] is protocol.buffer
    assert cb.calls[0][1] is protocol.writer

    # Clean up — fire EOF so the consumer task can exit.
    protocol.eof_received()
    transport._fire_connection_lost()
    await asyncio.wait_for(cb.consumer_done.wait(), timeout=1.0)


# ---------------------------------------------------------------------------
# data_received
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_data_received_feeds_buffer_so_consumer_can_read():
    cb = _ConsumerRecorder()
    protocol = _BlackBullProtocol(cb)
    transport = _FakeTransport()
    transport.set_protocol(protocol)

    protocol.connection_made(transport)
    await asyncio.sleep(0)   # let consumer suspend in read_until

    protocol.data_received(b'GET /healthz HTTP/1.1\r\nHost: x\r\n\r\n')

    await asyncio.wait_for(cb.consumer_done.wait(), timeout=1.0)
    assert cb.read_result == b'GET /healthz HTTP/1.1\r\n'


@pytest.mark.asyncio
async def test_data_received_before_connection_made_is_dropped():
    """Defensive: if data arrives before connection_made (shouldn't
    happen in production), the protocol doesn't crash — just drops."""
    cb = _ConsumerRecorder()
    protocol = _BlackBullProtocol(cb)
    # Don't call connection_made.
    protocol.data_received(b'orphan bytes')
    # No exception raised; buffer is still None.
    assert protocol.buffer is None


# ---------------------------------------------------------------------------
# eof_received — the critical path
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_eof_received_closes_transport_synchronously():
    """The architectural promise of Tier 1.5: eof_received MUST close
    the transport synchronously (in the same call), not schedule it
    for a later loop iteration."""
    cb = _ConsumerRecorder()
    protocol = _BlackBullProtocol(cb)
    transport = _FakeTransport()
    transport.set_protocol(protocol)

    protocol.connection_made(transport)
    await asyncio.sleep(0)

    assert transport.closed is False
    result = protocol.eof_received()
    # transport.close ran INSIDE eof_received — synchronously.
    assert transport.closed is True
    # And eof_received returns False to let asyncio fully close
    # (no half-open).
    assert result is False

    # Drive cleanup.
    transport._fire_connection_lost()
    await asyncio.wait_for(cb.consumer_done.wait(), timeout=1.0)


@pytest.mark.asyncio
async def test_eof_received_wakes_consumer_with_incomplete_read():
    """The consumer suspended in read_until should wake up with
    IncompleteReadError after eof_received fires — no extra loop
    iterations beyond the standard waiter resume."""
    cb = _ConsumerRecorder()
    protocol = _BlackBullProtocol(cb)
    transport = _FakeTransport()
    transport.set_protocol(protocol)

    protocol.connection_made(transport)
    await asyncio.sleep(0)   # let consumer suspend in read_until

    # No data, just an immediate EOF.
    protocol.eof_received()
    await asyncio.wait_for(cb.consumer_done.wait(), timeout=1.0)
    assert isinstance(cb.read_result, IncompleteReadError)

    # Cleanup.
    transport._fire_connection_lost()


@pytest.mark.asyncio
async def test_eof_received_with_buffered_partial_data():
    """If EOF arrives with partial data buffered (e.g. half a request
    line), the consumer sees IncompleteReadError with .partial set to
    the buffered bytes."""
    cb = _ConsumerRecorder()
    protocol = _BlackBullProtocol(cb)
    transport = _FakeTransport()
    transport.set_protocol(protocol)

    protocol.connection_made(transport)
    await asyncio.sleep(0)

    protocol.data_received(b'GET /partial ')   # no CRLF
    protocol.eof_received()

    await asyncio.wait_for(cb.consumer_done.wait(), timeout=1.0)
    assert isinstance(cb.read_result, IncompleteReadError)
    assert cb.read_result.partial == b'GET /partial '   # type: ignore[union-attr]

    transport._fire_connection_lost()


@pytest.mark.asyncio
async def test_eof_received_tolerates_transport_close_failure():
    """If transport.close() raises (e.g. TLS already aborted), the
    protocol still propagates EOF to the buffer and returns False —
    doesn't escape the exception upward."""
    cb = _ConsumerRecorder()
    protocol = _BlackBullProtocol(cb)

    class _FaultyTransport(_FakeTransport):
        def close(self) -> None:
            self.closed = True
            raise OSError('TLS aborted')

    transport = _FaultyTransport()
    transport.set_protocol(protocol)
    protocol.connection_made(transport)
    await asyncio.sleep(0)

    # Should NOT raise.
    result = protocol.eof_received()
    assert result is False
    # Buffer still got EOF.
    assert protocol.buffer is not None
    assert protocol.buffer.eof_received is True

    transport._fire_connection_lost()
    await asyncio.wait_for(cb.consumer_done.wait(), timeout=1.0)


# ---------------------------------------------------------------------------
# connection_lost
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_connection_lost_closes_buffer():
    """After connection_lost, the buffer is force-closed — any future
    feed() is ignored and any future suspend would immediately raise
    IncompleteReadError."""
    cb = _ConsumerRecorder()
    protocol = _BlackBullProtocol(cb)
    transport = _FakeTransport()
    transport.set_protocol(protocol)

    protocol.connection_made(transport)
    await asyncio.sleep(0)

    # Don't fire eof first — simulate abrupt connection loss.
    protocol.connection_lost(None)

    await asyncio.wait_for(cb.consumer_done.wait(), timeout=1.0)
    # The consumer was suspended in read_until; close() woke it.
    assert isinstance(cb.read_result, IncompleteReadError)


@pytest.mark.asyncio
async def test_connection_lost_after_eof_received_is_safe():
    """Normal close sequence: eof_received → connection_lost.  Both
    can run without issue."""
    cb = _ConsumerRecorder()
    protocol = _BlackBullProtocol(cb)
    transport = _FakeTransport()
    transport.set_protocol(protocol)

    protocol.connection_made(transport)
    await asyncio.sleep(0)

    protocol.eof_received()
    transport._fire_connection_lost()      # invokes protocol.connection_lost

    await asyncio.wait_for(cb.consumer_done.wait(), timeout=1.0)
    assert isinstance(cb.read_result, IncompleteReadError)


# ---------------------------------------------------------------------------
# Integration — realistic HTTP/1.1 request flow
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_realistic_request_response_cycle():
    """End-to-end inside one protocol instance: data_received feeds a
    request, the consumer reads + responds via the writer, then EOF
    arrives and the consumer cleans up."""
    response_sent = asyncio.Event()

    async def consumer(buffer, writer):
        # Read request line + headers.
        rline = await buffer.read_until(b'\r\n')
        headers = await buffer.read_until(b'\r\n\r\n')
        assert rline == b'GET / HTTP/1.1\r\n'
        assert b'host:' in headers.lower()
        # Send a minimal response.
        writer.write(
            b'HTTP/1.1 200 OK\r\n'
            b'content-length: 2\r\n'
            b'\r\n'
            b'ok'
        )
        response_sent.set()
        # Wait for EOF on next-request readuntil.  Expected to raise
        # IncompleteReadError once the test calls eof_received(); we
        # swallow it because the consumer task should exit cleanly on
        # peer close (mirroring HTTP1Actor's break-on-EOF behaviour).
        try:
            await buffer.read_until(b'\r\n')
        except IncompleteReadError:
            pass

    protocol = _BlackBullProtocol(consumer)
    transport = _FakeTransport()
    transport.set_protocol(protocol)
    protocol.connection_made(transport)
    await asyncio.sleep(0)

    protocol.data_received(
        b'GET / HTTP/1.1\r\nHost: localhost\r\n\r\n')

    await asyncio.wait_for(response_sent.wait(), timeout=1.0)
    assert any(b'200 OK' in w for w in transport.writes)

    # Peer closes — eof_received SYNCHRONOUSLY closes transport.
    assert transport.closed is False
    protocol.eof_received()
    assert transport.closed is True

    transport._fire_connection_lost()
    # Consumer task should now finish.
    await asyncio.sleep(0)
    assert protocol.task is not None
    assert protocol.task.done()
