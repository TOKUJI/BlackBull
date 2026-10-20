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
from blackbull.server.connection_protocol import BufferReader
from blackbull.server.recipient import HTTP1Recipient
from blackbull.server.http1_actor import request_framing

from tests.harness import fed

SLICE = 4096


@pytest.fixture
def reads(monkeypatch):
    """Every size the recipient asks the production reader for."""
    seen: list[int] = []
    for name in ('read', 'readexactly'):
        original = getattr(BufferReader, name)

        async def recording(self, n, _original=original):
            seen.append(n)
            return await _original(self, n)
        monkeypatch.setattr(BufferReader, name, recording)
    return seen


def _chunked_conn() -> Connection:
    return Connection(
        type='http', http_version='1.1', method='POST', path='/upload',
        raw_path=b'/upload', query_string=b'', headers=Headers(
            [(b'transfer-encoding', b'chunked')]),
        scheme='http',
    )


def _recipient(src) -> HTTP1Recipient:
    return HTTP1Recipient(src, _chunked_conn(), chunk_size=SLICE, framing=request_framing(_chunked_conn().headers))


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
    src = fed(f'{len(payload):x}\r\n'.encode() + payload + b'\r\n0\r\n\r\n').reader
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
async def test_no_read_is_larger_than_the_slice_however_large_the_chunk(reads):
    """The security property, stated as the reader sees it.

    A peer declaring 1 GiB must not make the server ask for 1 GiB.  Asking
    would both allocate on the peer's say-so and force the transport back
    open, since a read above the high-water mark cannot be satisfied while
    the pause it triggered is still in effect.
    """
    payload = b'x' * (SLICE * 2)
    src = fed(b'40000000\r\n' + payload).reader            # declares 1 GiB, sends 8 KiB
    r = _recipient(src)

    # Two full slices are available and must be delivered before the
    # shortfall is discovered — progress does not wait for the whole chunk.
    assert await r.next_chunk() == payload[:SLICE]
    assert await r.next_chunk() == payload[SLICE:]

    assert reads, 'no read was recorded'
    assert max(reads) <= SLICE, (
        f'asked the reader for {max(reads)} bytes on a peer-declared '
        f'chunk size; the slice is {SLICE}'
    )


@pytest.mark.asyncio
async def test_the_chunk_terminator_is_still_verified_across_slices():
    """Slicing must not lose the CRLF check — that check is anti-smuggling."""
    payload = b'y' * (SLICE + 10)
    src = fed(f'{len(payload):x}\r\n'.encode() + payload + b'XX').reader  # not CRLF
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
    src = fed(f'{len(first):x}\r\n'.encode() + first + b'\r\n'
        + b'3\r\nend\r\n0\r\n\r\n').reader
    r = _recipient(src)

    assert await _drain_body(r) == first + b'end'
    assert not r.framing_broken


@pytest.mark.asyncio
async def test_drain_enforces_its_cap_within_one_oversized_chunk(reads):
    """``drain`` bounds what an ignored body may buffer.

    Its ``max_bytes`` is checked per chunk returned, so a single chunk larger
    than the cap used to be buffered whole before the cap was consulted —
    the cap bounded the report, not the allocation.
    """
    payload = b'z' * (SLICE * 4)
    src = fed(f'{len(payload):x}\r\n'.encode() + payload + b'\r\n0\r\n\r\n').reader
    r = _recipient(src)

    assert await r.drain(max_bytes=SLICE) is False, (
        'a body over the cap must report False so the actor closes'
    )
    assert max(reads) <= SLICE
