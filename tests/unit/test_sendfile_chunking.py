"""``AsyncioWriter.sendfile`` hands the kernel bounded chunks.

One ``loop.sendfile`` call for the whole file is unbounded in two ways that
matter.  CPython's ``_sendfile_native`` ``pause_reading()``s the transport for
the duration, and — the reason this changed — nothing bounds how long the call
may take, so ``BB_WRITE_TIMEOUT`` covered every response body *except* a static
file.  A client that reads a large file one byte per second could hold the
connection, its FD, and its transport indefinitely, which is exactly the
slow-read slowloris shape the write timeout exists to stop.

Chunking is what makes the bound expressible: "send this 4 GiB file within 30
seconds" is not a policy anyone can set, but "make a megabyte of progress
within 30 seconds" is.

The chunk is large enough that ordinary web assets still go out in a single
call — ``test_a_small_file_is_still_one_call`` is the guard on that.
"""
from __future__ import annotations

import asyncio

import pytest

from blackbull.server.sender import _SENDFILE_CHUNK, AsyncioWriter

pytestmark = pytest.mark.asyncio


class _FakeTransport:
    def __init__(self) -> None:
        self.closed = False

    def close(self): self.closed = True
    def is_closing(self): return self.closed


class _FakeStreamWriter:
    """The asyncio StreamWriter surface ``AsyncioWriter`` actually uses."""

    def __init__(self) -> None:
        self.transport = _FakeTransport()
        self.drains = 0
        self.closed = False

    def write(self, data): pass
    def writelines(self, parts): pass

    async def drain(self):
        self.drains += 1

    def close(self):
        self.closed = True
        self.transport.close()


class _RecordingLoopSendfile:
    """Stands in for ``loop.sendfile``; records every (offset, count) asked
    for and reports the whole request as sent."""

    def __init__(self, short_by: int = 0) -> None:
        self.calls: list[tuple[int, int]] = []
        self._short_by = short_by

    async def __call__(self, transport, file, offset, count):
        self.calls.append((offset, count))
        # A short write on the first call only — the kernel is entitled to
        # send less than asked.
        if self._short_by and len(self.calls) == 1:
            return max(count - self._short_by, 0)
        return count


@pytest.fixture()
def writer():
    return AsyncioWriter(_FakeStreamWriter())


def _patch_sendfile(monkeypatch, impl):
    monkeypatch.setattr(asyncio.get_event_loop(), 'sendfile', impl,
                        raising=False)
    return impl


# ---------------------------------------------------------------------------
# Chunking
# ---------------------------------------------------------------------------

async def test_a_small_file_is_still_one_call(writer, monkeypatch):
    """The common case — a web asset well under the chunk — must not pay for
    a loop it does not need."""
    rec = _patch_sendfile(monkeypatch, _RecordingLoopSendfile())
    size = 64 * 1024

    assert await writer.sendfile(object(), 0, size) == size
    assert rec.calls == [(0, size)]


async def test_a_large_file_is_split_into_bounded_calls(writer, monkeypatch):
    rec = _patch_sendfile(monkeypatch, _RecordingLoopSendfile())
    size = _SENDFILE_CHUNK * 3 + 12345

    assert await writer.sendfile(object(), 0, size) == size
    assert all(count <= _SENDFILE_CHUNK for _, count in rec.calls)
    assert sum(count for _, count in rec.calls) == size


async def test_chunk_offsets_are_contiguous_and_start_at_the_caller_offset(
        writer, monkeypatch):
    """Off-by-one here silently corrupts or duplicates file content, which no
    status code would reveal."""
    rec = _patch_sendfile(monkeypatch, _RecordingLoopSendfile())
    start, size = 4096, _SENDFILE_CHUNK * 2 + 7

    await writer.sendfile(object(), start, size)

    expected_offset = start
    for offset, count in rec.calls:
        assert offset == expected_offset
        expected_offset += count
    assert expected_offset == start + size


async def test_a_short_kernel_write_resumes_from_what_was_sent(writer, monkeypatch):
    """``os.sendfile`` may send less than asked; the next chunk starts where
    the last one stopped, not where it was asked to stop."""
    rec = _patch_sendfile(monkeypatch, _RecordingLoopSendfile(short_by=1000))
    size = _SENDFILE_CHUNK * 2

    assert await writer.sendfile(object(), 0, size) == size
    assert rec.calls[1][0] == _SENDFILE_CHUNK - 1000


async def test_a_peer_that_stops_accepting_ends_the_transfer(writer, monkeypatch):
    """A zero-byte result is EOF on the socket, not an invitation to spin."""
    async def _sends_nothing(transport, file, offset, count):
        return 0

    _patch_sendfile(monkeypatch, _sends_nothing)
    assert await writer.sendfile(object(), 0, _SENDFILE_CHUNK * 4) == 0


async def test_an_unsupported_transport_still_reports_itself(writer, monkeypatch):
    """``_pathsend`` catches this to run its read+write fallback; swallowing it
    inside the chunk loop would serve a truncated file instead."""
    async def _unsupported(transport, file, offset, count):
        raise NotImplementedError('no sendfile here')

    _patch_sendfile(monkeypatch, _unsupported)
    with pytest.raises(NotImplementedError):
        await writer.sendfile(object(), 0, 4096)


# ---------------------------------------------------------------------------
# The write deadline, which chunking is what makes enforceable
# ---------------------------------------------------------------------------

async def test_a_stalled_chunk_trips_the_write_timeout():
    """The gap this change closes: a stalled static-file transfer used to be
    the one write path ``BB_WRITE_TIMEOUT`` could not reach."""
    sw = _FakeStreamWriter()
    w = AsyncioWriter(sw, write_timeout=0.05)

    async def _never_completes(transport, file, offset, count):
        await asyncio.sleep(3600)

    asyncio.get_event_loop().sendfile = _never_completes  # type: ignore[method-assign]
    try:
        with pytest.raises(ConnectionResetError, match='write timeout'):
            await asyncio.wait_for(w.sendfile(object(), 0, _SENDFILE_CHUNK * 2),
                                   timeout=5)
    finally:
        del asyncio.get_event_loop().sendfile  # type: ignore[attr-defined]

    assert sw.closed, 'a timed-out transfer must release the connection'


async def test_the_deadline_is_per_chunk_not_per_transfer(monkeypatch):
    """A large file that keeps making progress is not cut off for being
    large — each chunk gets the budget afresh."""
    sw = _FakeStreamWriter()
    w = AsyncioWriter(sw, write_timeout=0.3)
    chunks = 4

    async def _slow_but_progressing(transport, file, offset, count):
        await asyncio.sleep(0.1)
        return count

    _patch_sendfile(monkeypatch, _slow_but_progressing)
    size = _SENDFILE_CHUNK * chunks
    # Total transfer time (~0.4s) exceeds the 0.3s budget; no single chunk does.
    assert await w.sendfile(object(), 0, size) == size
    assert not sw.closed


async def test_the_pre_transfer_header_drain_is_bounded():
    """Headers are flushed before the file bytes.  That drain used to be the
    raw ``drain()``, outside the timeout that guards every other write."""
    sw = _FakeStreamWriter()
    w = AsyncioWriter(sw, write_timeout=0.05)

    async def _stalled_drain():
        await asyncio.sleep(3600)

    sw.drain = _stalled_drain  # type: ignore[method-assign]
    with pytest.raises(ConnectionResetError, match='write timeout'):
        await asyncio.wait_for(w.sendfile(object(), 0, 4096), timeout=5)
    assert sw.closed
