"""Unit tests for PrefixReader — the peek-and-replay primitive for the
decouple-connection-detection refactor (Stage 1).

A PrefixReader replays an already-read prefix, then falls through to the
underlying reader, using its fast native readuntil/readexactly once the prefix
is drained — including the seam case where the separator straddles the
prefix/underlying boundary.
"""
import asyncio

import pytest

from blackbull.server.recipient import (
    AbstractReader, AsyncioReader, IncompleteReadError, PrefixReader,
    ReadLimitExceeded,
)

pytestmark = pytest.mark.asyncio


class _Under(AbstractReader):
    """Buffer-backed underlying reader with native readuntil/readexactly."""

    def __init__(self, data: bytes = b'', eof: bool = True) -> None:
        self.buf = bytearray(data)
        self._eof = eof

    async def read(self, n: int) -> bytes:
        chunk = bytes(self.buf[:n])
        del self.buf[:n]
        return chunk

    async def readuntil(self, sep: bytes) -> bytes:
        idx = self.buf.find(sep)
        if idx == -1:
            raise IncompleteReadError()
        end = idx + len(sep)
        out = bytes(self.buf[:end])
        del self.buf[:end]
        return out

    async def readexactly(self, n: int) -> bytes:
        if len(self.buf) < n:
            raise IncompleteReadError()
        out = bytes(self.buf[:n])
        del self.buf[:n]
        return out

    def at_eof(self) -> bool:
        return self._eof and not self.buf


async def test_read_drains_prefix_then_underlying():
    pr = PrefixReader(b'AB', _Under(b'CD'))
    assert await pr.read(1) == b'A'
    assert await pr.read(10) == b'B'        # rest of prefix
    assert await pr.read(10) == b'CD'       # falls through
    assert await pr.read(10) == b''


@pytest.mark.parametrize('prefix,under,n1,r1,n2,r2', [
    pytest.param(b'HELLO', b'WORLD', 3, b'HEL', 2, b'LO', id='within-prefix'),
    pytest.param(b'AB', b'CDEF', 4, b'ABCD', 2, b'EF', id='spans-boundary'),
])
async def test_readexactly_within_prefix(prefix, under, n1, r1, n2, r2):
    pr = PrefixReader(prefix, _Under(under))
    assert await pr.readexactly(n1) == r1
    assert await pr.readexactly(n2) == r2


async def test_readuntil_sep_in_underlying():
    pr = PrefixReader(b'GET ', _Under(b'/ HTTP/1.1\r\nrest'))
    assert await pr.readuntil(b'\r\n') == b'GET / HTTP/1.1\r\n'
    assert await pr.read(4) == b'rest'


@pytest.mark.parametrize('prefix,under,r1,r2', [
    pytest.param(b'one\r\ntwo', b'three\r\n', b'one\r\n', b'twothree\r\n',
                 id='sep-in-prefix'),
    pytest.param(b'GET / HTTP/1.1\r', b'\nHost: x\r\n',
                 b'GET / HTTP/1.1\r\n', b'Host: x\r\n',
                 id='sep-straddles-boundary'),
])
async def test_readuntil_sep_straddles_boundary(prefix, under, r1, r2):
    pr = PrefixReader(prefix, _Under(under))
    assert await pr.readuntil(b'\r\n') == r1
    # the over-read underlying bytes were pushed back, not lost
    assert await pr.readuntil(b'\r\n') == r2


async def test_limited_readuntil_handles_separator_at_boundary():
    pr = PrefixReader(b'abc\r', _Under(b'\n', eof=True))

    assert await pr.readuntil(b'\r\n', limit=5) == b'abc\r\n'


async def test_limited_readuntil_preserves_prefix_on_overrun():
    pr = PrefixReader(b'123456', _Under(b'\n', eof=True))

    with pytest.raises(ReadLimitExceeded) as caught:
        await pr.readuntil(b'\n', limit=5)

    assert caught.value.seen == b'123456'
    assert pr.buffered_len() == 6


async def test_limited_readuntil_empty_prefix_preserves_underlying_overrun():
    sr = asyncio.StreamReader()
    sr.feed_data(b'x' * 10_000 + b'\n')
    sr.feed_eof()
    pr = PrefixReader(b'', AsyncioReader(sr))

    with pytest.raises(ReadLimitExceeded):
        await pr.readuntil(b'\n', limit=5)

    assert pr.buffered_len() == 10_001


async def test_limited_readuntil_legacy_underlying_replays_overrun():
    underlying = _Under(b'x' * 20 + b'\n')
    pr = PrefixReader(b'', underlying)

    with pytest.raises(ReadLimitExceeded):
        await pr.readuntil(b'\n', limit=5)

    assert pr.peek(6) == b'x' * 6
    assert pr.buffered_len() == 6
    assert len(underlying.buf) == 15


async def test_limited_readuntil_handles_overlapping_separator_at_boundary():
    sr = asyncio.StreamReader()
    sr.feed_data(b'\r\n')
    sr.feed_eof()
    pr = PrefixReader(b'abc\r', AsyncioReader(sr))

    assert await pr.readuntil(b'\r\n', limit=6) == b'abc\r\r\n'


async def test_limited_readuntil_replays_consumed_seam_bytes_on_overrun():
    sr = asyncio.StreamReader()
    sr.feed_data(b'\r' + b'x' * 20 + b'\n')
    sr.feed_eof()
    pr = PrefixReader(b'abc\r', AsyncioReader(sr))

    with pytest.raises(ReadLimitExceeded) as caught:
        await pr.readuntil(b'\r\n', limit=6)

    assert caught.value.seen.startswith(b'abc\r\r')
    assert pr.peek(7).startswith(b'abc\r\rxx')


async def test_at_eof_reflects_prefix_and_underlying():
    pr = PrefixReader(b'X', _Under(b'', eof=True))
    assert pr.at_eof() is False          # prefix not drained yet
    assert await pr.read(1) == b'X'
    assert pr.at_eof() is True           # prefix drained + underlying at eof
