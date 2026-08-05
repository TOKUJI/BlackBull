"""``BufferedH1Reader`` — the buffer half of the H/1.1 protocol-mode front end.

The two properties worth pinning are the ones the streams front end gets for
free and a scanning front end has to earn: **nothing is lost to over-reading**,
and **nothing suspends when the answer is already buffered**.
"""
import pytest

from blackbull.server.h1_buffer import LIMIT_EXCEEDED, BufferedH1Reader
from blackbull.server.recipient import IncompleteReadError


def _conn(headers, path: str = '/'):
    """The native ``Connection`` the actor binds a recipient to.

    ``HTTP1Recipient`` frames from ``conn.headers``; it has no scope-dict
    shape, so a test that drives it directly builds the same object the
    parser would.
    """
    from blackbull.connection import Connection
    from blackbull.headers import Headers
    return Connection(method='POST', path=path, raw_path=path.encode(),
                      headers=headers if isinstance(headers, Headers)
                      else Headers(headers), type='http')


class _FakeReader:
    """Hands out pre-scripted chunks, one per ``read`` call.

    Chunk boundaries are the point: a reader that always returns the whole
    request in one call cannot exercise a delimiter split across two reads.

    Implements the whole ``AbstractReader`` surface, because
    :class:`BufferedH1Reader` delegates to the underlying reader whenever its
    own buffer is empty — a double that only answers ``read`` would silently
    exercise just the interposing half.
    """

    def __init__(self, *chunks: bytes):
        self._chunks = list(chunks)
        self.read_calls = 0

    async def read(self, n: int = -1) -> bytes:
        self.read_calls += 1
        if n < 0:
            out = b''.join(self._chunks)
            self._chunks = []
            return out
        return self._chunks.pop(0) if self._chunks else b''

    async def readexactly(self, n: int) -> bytes:
        buf = bytearray()
        while len(buf) < n:
            chunk = await self.read(65536)
            if not chunk:
                raise IncompleteReadError(bytes(buf))
            buf += chunk
        self._chunks.insert(0, bytes(buf[n:]))
        return bytes(buf[:n])

    async def readuntil(self, sep: bytes = b'\n') -> bytes:
        buf = bytearray()
        while sep not in buf:
            chunk = await self.read(65536)
            if not chunk:
                raise IncompleteReadError(bytes(buf))
            buf += chunk
        end = buf.find(sep) + len(sep)
        self._chunks.insert(0, bytes(buf[end:]))
        return bytes(buf[:end])


@pytest.mark.asyncio
async def test_surplus_past_the_delimiter_is_retained():
    """The over-read that makes a single scan possible must not lose bytes."""
    r = BufferedH1Reader(_FakeReader(b'HEAD\r\n\r\nBODYBYTES'))
    idx = await r.fill_until(b'\r\n\r\n')
    head = await r.readexactly(idx + 4)
    assert head == b'HEAD\r\n\r\n'
    assert await r.read(-1) == b'BODYBYTES'


@pytest.mark.asyncio
async def test_delimiter_split_across_two_reads_is_found():
    """TCP may split anywhere, including through the terminator itself."""
    r = BufferedH1Reader(_FakeReader(b'GET / HTTP/1.1\r\n\r', b'\nrest'))
    assert await r.fill_until(b'\r\n\r\n') == 14


@pytest.mark.asyncio
async def test_delimiter_split_one_byte_per_read():
    """The pathological fragmentation the HttpArena validator probes."""
    payload = b'GET / HTTP/1.1\r\nH: v\r\n\r\n'
    r = BufferedH1Reader(_FakeReader(*[payload[i:i + 1] for i in range(len(payload))]))
    assert await r.fill_until(b'\r\n\r\n') == len(payload) - 4


@pytest.mark.asyncio
async def test_buffered_answer_costs_no_read_call():
    """Keep-alive residency: a second head already in hand must not re-read.

    ``read_calls`` is the observable proxy for a loop suspension — an await
    that resolves from the buffer never reaches the transport.
    """
    r = BufferedH1Reader(_FakeReader(b'A\r\n\r\nB\r\n\r\n'))
    await r.readexactly(await r.fill_until(b'\r\n\r\n') + 4)
    calls_after_first = r.read_calls
    idx = await r.fill_until(b'\r\n\r\n')          # second head, same buffer
    assert idx == 1
    assert r.read_calls == calls_after_first, 'second head should not re-read'


@pytest.mark.asyncio
async def test_limit_stops_an_endless_head():
    """Without the bound, a peer that never terminates the head grows the
    buffer until the process dies — the slow-loris shape."""
    r = BufferedH1Reader(_FakeReader(*[b'x' * 100] * 50))
    assert await r.fill_until(b'\r\n\r\n', limit=200) == LIMIT_EXCEEDED


@pytest.mark.asyncio
async def test_eof_without_delimiter_reports_minus_one():
    r = BufferedH1Reader(_FakeReader(b'GET / HTTP/1.1\r\n'))
    assert await r.fill_until(b'\r\n\r\n') == -1


@pytest.mark.asyncio
async def test_unread_puts_bytes_back_in_front():
    r = BufferedH1Reader(_FakeReader(b'DEF'))
    await r.read(1)
    r.unread(b'ABC')
    assert await r.read(3) == b'ABC'


@pytest.mark.asyncio
async def test_readexactly_short_read_raises_runtime_agnostic_error():
    """Must raise the AbstractReader error, not asyncio's — the actor catches
    the former and would otherwise let a real EOF escape as an unknown type."""
    r = BufferedH1Reader(_FakeReader(b'AB'))
    with pytest.raises(IncompleteReadError):
        await r.readexactly(10)


@pytest.mark.asyncio
async def test_read_all_drains_buffer_and_stream():
    r = BufferedH1Reader(_FakeReader(b'AB', b'CD', b'EF'))
    assert await r.read(-1) == b'ABCDEF'
    assert await r.read(-1) == b''


def test_counts_as_an_abstract_reader():
    """``RecipientFactory.http1`` wraps anything that is not an
    :class:`AbstractReader` in an :class:`AsyncioReader` — **per request**.

    ``BufferedH1Reader`` already presents the whole surface, so failing that
    check bought nothing but an allocation on every request of every keep-alive
    connection, paid only on the front end that exists to remove per-request
    work.  Registration is what keeps the recipient talking to the buffer
    directly.
    """
    from blackbull.server.recipient import AbstractReader

    assert isinstance(BufferedH1Reader(_FakeReader(b'')), AbstractReader)


def test_recipient_factory_does_not_rewrap_it():
    """The property the registration exists for, at the call site that pays."""
    from blackbull.server.recipient import RecipientFactory

    reader = BufferedH1Reader(_FakeReader(b''))
    recipient = RecipientFactory.http1(
        reader, _conn([], '/'))

    assert recipient._reader is reader
