"""A chunked body is read in bounded slices, whatever size the peer declares.

``chunk-size`` is a number the *peer* writes.  Reading a whole chunk in one
``readexactly`` therefore lets the peer choose how much the server buffers —
and it defeats backpressure rather than merely straining it, because a read
larger than the high-water mark has to resume the paused transport to make
progress (otherwise it deadlocks against its own pause).

So the invariant is not "the declared size is sane"; it is that **no single
read is larger than the configured slice**, which is what the Content-Length
path has always done.  Every test here asserts that through the bytes actually
requested from the reader, not through a limit constant.
"""
import pytest

from blackbull.connection import Connection
from blackbull.headers import Headers
from blackbull.router import HTTPException
from blackbull.server.recipient import AsyncioReader, HTTP1Recipient

SLICE = 4096


class _RecordingSource:
    """Byte source that records the size of every exact-read it is asked for."""

    def __init__(self, data: bytes = b''):
        self._d = bytearray(data)
        self.exact_reads: list[int] = []

    def feed(self, data: bytes) -> None:
        self._d += data

    async def read(self, n: int = -1) -> bytes:
        if n < 0:
            out, self._d = bytes(self._d), bytearray()
            return out
        out = bytes(self._d[:n])
        del self._d[:n]
        return out

    async def readuntil(self, sep: bytes = b'\n') -> bytes:
        idx = self._d.find(sep)
        if idx == -1:
            from blackbull.server.recipient import IncompleteReadError
            out, self._d = bytes(self._d), bytearray()
            raise IncompleteReadError(out)
        end = idx + len(sep)
        out = bytes(self._d[:end])
        del self._d[:end]
        return out

    async def readexactly(self, n: int) -> bytes:
        self.exact_reads.append(n)
        if len(self._d) < n:
            from blackbull.server.recipient import IncompleteReadError
            out, self._d = bytes(self._d), bytearray()
            raise IncompleteReadError(out)
        out = bytes(self._d[:n])
        del self._d[:n]
        return out


def _chunked_conn() -> Connection:
    return Connection(
        type='http', http_version='1.1', method='POST', path='/upload',
        raw_path=b'/upload', query_string=b'', headers=Headers(
            [(b'transfer-encoding', b'chunked')]),
        scheme='http',
    )


def _recipient(src: _RecordingSource) -> HTTP1Recipient:
    return HTTP1Recipient(AsyncioReader(src), _chunked_conn(), chunk_size=SLICE)


async def _drain_body(r: HTTP1Recipient) -> bytes:
    body = b''
    while True:
        chunk = await r.next_chunk()
        if chunk is None:
            return body
        body += chunk


@pytest.mark.asyncio
async def test_a_chunk_larger_than_the_slice_arrives_in_slices():
    """One big chunk, several events — and the body is byte-identical."""
    payload = bytes(range(256)) * 40          # 10240 B, 2.5 slices
    src = _RecordingSource(
        f'{len(payload):x}\r\n'.encode() + payload + b'\r\n0\r\n\r\n')
    r = _recipient(src)

    slices = []
    while True:
        chunk = await r.next_chunk()
        if chunk is None:
            break
        slices.append(chunk)

    assert b''.join(slices) == payload
    assert len(slices) == 3, f'expected 3 slices of <={SLICE}, got {len(slices)}'
    assert max(len(s) for s in slices) <= SLICE


@pytest.mark.asyncio
async def test_no_read_is_larger_than_the_slice_however_large_the_chunk():
    """The security property, stated as the reader sees it.

    A peer declaring 1 GiB must not make the server ask for 1 GiB.  Asking
    would both allocate on the peer's say-so and force the transport back
    open, since a read above the high-water mark cannot be satisfied while
    the pause it triggered is still in effect.
    """
    payload = b'x' * (SLICE * 2)
    src = _RecordingSource(
        b'40000000\r\n' + payload)            # declares 1 GiB, sends 8 KiB
    r = _recipient(src)

    # Two full slices are available and must be delivered before the
    # shortfall is discovered — progress does not wait for the whole chunk.
    assert await r.next_chunk() == payload[:SLICE]
    assert await r.next_chunk() == payload[SLICE:]

    assert src.exact_reads, 'no exact-read was recorded'
    assert max(src.exact_reads) <= SLICE, (
        f'asked the reader for {max(src.exact_reads)} bytes on a peer-declared '
        f'chunk size; the slice is {SLICE}'
    )


@pytest.mark.asyncio
async def test_the_chunk_terminator_is_still_verified_across_slices():
    """Slicing must not lose the CRLF check — that check is anti-smuggling."""
    payload = b'y' * (SLICE + 10)
    src = _RecordingSource(
        f'{len(payload):x}\r\n'.encode() + payload + b'XX')  # not CRLF
    r = _recipient(src)

    with pytest.raises(HTTPException):
        await _drain_body(r)
    assert r.framing_broken, (
        'a bad chunk terminator must desync the stream so the connection '
        'closes instead of keep-aliving'
    )


@pytest.mark.asyncio
async def test_framing_continues_after_a_sliced_chunk():
    """The next chunk parses: slicing left the stream positioned correctly."""
    first = b'a' * (SLICE + 1)
    src = _RecordingSource(
        f'{len(first):x}\r\n'.encode() + first + b'\r\n'
        + b'3\r\nend\r\n0\r\n\r\n')
    r = _recipient(src)

    assert await _drain_body(r) == first + b'end'
    assert not r.framing_broken


@pytest.mark.asyncio
async def test_drain_enforces_its_cap_within_one_oversized_chunk():
    """``drain`` bounds what an ignored body may buffer.

    Its ``max_bytes`` is checked per chunk returned, so a single chunk larger
    than the cap used to be buffered whole before the cap was consulted —
    the cap bounded the report, not the allocation.
    """
    payload = b'z' * (SLICE * 4)
    src = _RecordingSource(
        f'{len(payload):x}\r\n'.encode() + payload + b'\r\n0\r\n\r\n')
    r = _recipient(src)

    assert await r.drain(max_bytes=SLICE) is False, (
        'a body over the cap must report False so the actor closes'
    )
    assert max(src.exact_reads) <= SLICE
