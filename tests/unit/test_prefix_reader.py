"""Unit tests for PrefixReader — the peek-and-replay primitive for the
decouple-connection-detection refactor (Stage 1).

A PrefixReader replays an already-read prefix, then falls through to the
underlying reader, using its fast native readuntil/readexactly once the prefix
is drained — including the seam case where the separator straddles the
prefix/underlying boundary.
"""
import pytest

from blackbull.server.recipient import (
    AbstractReader, IncompleteReadError, PrefixReader,
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


async def test_readexactly_within_prefix():
    pr = PrefixReader(b'HELLO', _Under(b'WORLD'))
    assert await pr.readexactly(3) == b'HEL'
    assert await pr.readexactly(2) == b'LO'


async def test_readexactly_spans_boundary():
    pr = PrefixReader(b'AB', _Under(b'CDEF'))
    assert await pr.readexactly(4) == b'ABCD'   # 2 from prefix + 2 native
    assert await pr.readexactly(2) == b'EF'


async def test_readuntil_sep_in_prefix():
    pr = PrefixReader(b'one\r\ntwo', _Under(b'three\r\n'))
    assert await pr.readuntil(b'\r\n') == b'one\r\n'
    # leftover prefix 'two' + underlying serve the next line
    assert await pr.readuntil(b'\r\n') == b'twothree\r\n'


async def test_readuntil_sep_in_underlying():
    pr = PrefixReader(b'GET ', _Under(b'/ HTTP/1.1\r\nrest'))
    assert await pr.readuntil(b'\r\n') == b'GET / HTTP/1.1\r\n'
    assert await pr.read(4) == b'rest'


async def test_readuntil_sep_straddles_boundary():
    # Prefix ends with the first separator byte; the second is the first
    # underlying byte — the separator straddles the seam.
    pr = PrefixReader(b'GET / HTTP/1.1\r', _Under(b'\nHost: x\r\n'))
    assert await pr.readuntil(b'\r\n') == b'GET / HTTP/1.1\r\n'
    # the over-read underlying bytes were pushed back, not lost
    assert await pr.readuntil(b'\r\n') == b'Host: x\r\n'


async def test_at_eof_reflects_prefix_and_underlying():
    pr = PrefixReader(b'X', _Under(b'', eof=True))
    assert pr.at_eof() is False          # prefix not drained yet
    assert await pr.read(1) == b'X'
    assert pr.at_eof() is True           # prefix drained + underlying at eof
