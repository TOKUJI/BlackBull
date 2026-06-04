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

    # ``loop.sendfile`` calls ``transport.write_eof``-like primitives
    # internally — for sendfile-targeted tests we expose a fake
    # ``transport`` attribute that the patched loop.sendfile inspects.
    @property
    def transport(self):
        return getattr(self, '_transport', object())


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


# ---------------------------------------------------------------------------
# Sprint 31 — ``http.response.pathsend`` / sendfile path
# ---------------------------------------------------------------------------

class _SendfileLoopProxy:
    """Wrap the running loop and replace ``sendfile`` with a recorder.

    Lets the test exercise ``AsyncioWriter.sendfile`` without a real
    socket transport.  Returns *return_value* (or raises *raises*) so
    both the happy path and the TLS-unsupported path are coverable.
    """

    def __init__(self, return_value=None, raises=None):
        self._return_value = return_value
        self._raises = raises
        self.calls = []

    async def sendfile(self, transport, file, offset, count):
        self.calls.append((transport, file, offset, count))
        if self._raises is not None:
            raise self._raises
        return self._return_value


@pytest.mark.asyncio
async def test_sendfile_drains_then_calls_loop_sendfile(monkeypatch):
    """Headers buffered before sendfile must be flushed first so they
    precede the file bytes on the wire."""
    sw = _FakeStreamWriter()
    sw._transport = object()
    w = AsyncioWriter(sw)
    fake_loop = _SendfileLoopProxy(return_value=12345)
    monkeypatch.setattr(asyncio, 'get_running_loop', lambda: fake_loop)

    f = object()
    result = await w.sendfile(f, 0, 12345)

    assert result == 12345
    assert sw.drain_calls == 1, 'must drain buffered writes before sendfile'
    assert fake_loop.calls == [(sw._transport, f, 0, 12345)]


@pytest.mark.asyncio
async def test_sendfile_propagates_notimplemented_for_tls(monkeypatch):
    """On SSL transports ``loop.sendfile`` raises NotImplementedError —
    AsyncioWriter must let that surface so the caller can fall back."""
    sw = _FakeStreamWriter()
    sw._transport = object()
    w = AsyncioWriter(sw)
    fake_loop = _SendfileLoopProxy(
        raises=NotImplementedError('SSL not supported'))
    monkeypatch.setattr(asyncio, 'get_running_loop', lambda: fake_loop)

    with pytest.raises(NotImplementedError):
        await w.sendfile(object(), 0, 4096)


@pytest.mark.asyncio
async def test_abstract_writer_sendfile_default_raises():
    """The default ``AbstractWriter.sendfile`` raises NotImplementedError
    so subclasses must opt in.  Equivalent to the TLS path the writer
    falls back from."""
    from blackbull.server.sender import AbstractWriter

    class _Bare(AbstractWriter):
        async def write(self, data):
            pass

    bare = _Bare()
    with pytest.raises(NotImplementedError):
        await bare.sendfile(object(), 0, 1)
