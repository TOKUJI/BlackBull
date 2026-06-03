"""Unit tests for AsyncioWriter — focused on the ``write_timeout``
slow-read defence introduced in Sprint 30 Tier 1.1.

The defence: a peer that reads the response 1 byte/sec eventually fills
the kernel send buffer; ``StreamWriter.drain()`` then blocks indefinitely
waiting for the peer's TCP window to reopen.  Without ``write_timeout``
the server's write coroutine is parked forever, holding the connection
slot.  With ``write_timeout`` we bound that wait and surface the
failure as a peer disconnect.
"""
from __future__ import annotations

import asyncio

import pytest

from blackbull.server.sender import AsyncioWriter


class _FakeStreamWriter:
    """Minimal asyncio.StreamWriter-shaped double for AsyncioWriter tests.

    Records what was written and lets the test control drain()'s
    behaviour (immediate, slow, raising).
    """

    def __init__(self, drain_behaviour='immediate'):
        self.drain_behaviour = drain_behaviour
        self.written: list[bytes] = []
        self.drain_calls: int = 0
        self.closed: bool = False

    def write(self, data: bytes) -> None:
        self.written.append(data)

    async def drain(self) -> None:
        self.drain_calls += 1
        if self.drain_behaviour == 'immediate':
            return
        if self.drain_behaviour == 'slow':
            # Park forever — simulates a slow-read peer that has filled
            # the kernel send buffer.
            await asyncio.Event().wait()
        if self.drain_behaviour == 'reset':
            raise ConnectionResetError('peer reset')
        raise AssertionError(f'unknown behaviour: {self.drain_behaviour}')

    def close(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None:  # pragma: no cover — close path
        return


@pytest.mark.asyncio
async def test_default_write_timeout_disabled_allows_slow_drain_to_complete():
    """write_timeout=0 (default) keeps the previous unbounded behaviour:
    a slow drain just makes the write hang.  No surprise behaviour change
    for code that doesn't opt into the timeout."""
    sw = _FakeStreamWriter(drain_behaviour='immediate')
    w = AsyncioWriter(sw)   # default timeout=0
    await w.write(b'ok')
    assert sw.written == [b'ok']
    assert sw.drain_calls == 1
    assert sw.closed is False


@pytest.mark.asyncio
async def test_write_timeout_fires_on_slow_drain_and_raises_reset():
    """With write_timeout set, a drain() that never completes triggers
    a TimeoutError internally, which AsyncioWriter converts to
    ConnectionResetError so the existing peer-disconnect handling
    in BaseSender catches it."""
    sw = _FakeStreamWriter(drain_behaviour='slow')
    w = AsyncioWriter(sw, write_timeout=0.05)   # 50ms — tight for tests
    with pytest.raises(ConnectionResetError, match='write timeout'):
        await w.write(b'too big to fit in send buffer')
    # Transport must have been closed so the FD is released.
    assert sw.closed is True


@pytest.mark.asyncio
async def test_write_timeout_doesnt_fire_when_drain_is_fast():
    """If drain returns promptly, the timeout never fires and write
    completes normally."""
    sw = _FakeStreamWriter(drain_behaviour='immediate')
    w = AsyncioWriter(sw, write_timeout=10.0)
    await w.write(b'fast')
    assert sw.written == [b'fast']
    assert sw.closed is False


@pytest.mark.asyncio
async def test_write_propagates_connection_reset_unchanged():
    """A ConnectionResetError from drain() (peer reset their side)
    should propagate without being mistaken for a timeout."""
    sw = _FakeStreamWriter(drain_behaviour='reset')
    w = AsyncioWriter(sw, write_timeout=10.0)
    with pytest.raises(ConnectionResetError, match='peer reset'):
        await w.write(b'lost')
    # Timeout did not fire, so the writer was NOT force-closed by us.
    # (The asyncio layer or upstream code handles transport teardown
    # on ConnectionResetError.)
    assert sw.closed is False


@pytest.mark.asyncio
async def test_write_timeout_constructor_arg_stored():
    """Smoke-test for the new constructor arg surface."""
    sw = _FakeStreamWriter()
    w = AsyncioWriter(sw, write_timeout=5.5)
    assert w._write_timeout == 5.5

    w_default = AsyncioWriter(sw)
    assert w_default._write_timeout == 0.0
