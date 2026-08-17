"""Transport-paced (up-to-n) body reads on the HTTP/1.1 ``Content-Length`` path.

Each read is up-to-n: it returns whatever the peer has delivered so far, up to
the per-read cap, never blocking for a fixed size.  The slice therefore
*follows the transport* — a slow peer yields small chunks, a fast one big
chunks up to the cap — and there is no sizing logic of our own to go wrong
(the adaptive grow/shrink machinery was rejected on measurement).

The exact-bytes contract lives at the *body* level: ``_content_length`` is
counted down by what each read returns, and EOF before it is spent is a
truncated upload, never a complete one.  Everything here is asserted through
the chunks the recipient hands back — the sizes *are* the observable, since
each one becomes an ``http.request`` event.
"""
import pytest

from blackbull.connection import Connection
from blackbull.headers import Headers
from blackbull.request import ClientDisconnected
from blackbull.server.recipient import AbstractReader, HTTP1Recipient


class _Source(AbstractReader):
    """A reader whose backlog is explicit: ``feed`` delivers and ``read``
    hands over at most what is resident.  The transport pace is whatever the
    test decides, and each read records what it was asked for and what it
    returned."""

    def __init__(self, data: bytes = b''):
        self._d = bytearray(data)
        self.reads: list[tuple[int, int]] = []   # (asked, returned)

    def feed(self, data: bytes) -> None:
        self._d += data

    async def read(self, n: int) -> bytes:
        out = bytes(self._d[:n])
        del self._d[:len(out)]
        self.reads.append((n, len(out)))
        return out

    async def readexactly(self, n: int) -> bytes:
        from blackbull.server.recipient import IncompleteReadError
        if len(self._d) < n:
            out = bytes(self._d)
            self._d.clear()
            raise IncompleteReadError(out)
        out = bytes(self._d[:n])
        del self._d[:n]
        self.reads.append((n, len(out)))
        return out


def _conn(body_len: int) -> Connection:
    return Connection(method='POST', path='/p', raw_path=b'/p',
                      headers=Headers([(b'content-length', str(body_len).encode())]),
                      type='http')


async def _drain(recipient: HTTP1Recipient) -> list[bytes]:
    out = []
    while (chunk := await recipient.next_chunk()) is not None:
        out.append(chunk)
    return out


@pytest.mark.asyncio
async def test_reads_follow_the_transport():
    """A fast peer earns large up-to-n slices; nothing waits on the cap."""
    src = _Source()
    r = HTTP1Recipient(src, _conn(100_000), chunk_max=512 * 1024)
    src.feed(b'a' * 64_000)            # first arrival: 64 KiB resident
    assert await r.next_chunk() == b'a' * 64_000
    src.feed(b'b' * 36_000)            # the rest arrives in one go
    assert await _drain(r) == [b'b' * 36_000]


@pytest.mark.asyncio
async def test_slow_peer_gets_small_slices():
    """A trickle never blocks on the cap — each read returns what arrived."""
    src = _Source()
    r = HTTP1Recipient(src, _conn(8), chunk_max=8)
    src.feed(b'ab')
    assert await r.next_chunk() == b'ab'
    src.feed(b'c')
    assert await r.next_chunk() == b'c'
    src.feed(b'defgh')
    assert await r.next_chunk() == b'defgh'
    assert await r.next_chunk() is None


@pytest.mark.asyncio
async def test_read_asks_at_most_the_cap():
    """The per-read ask is min(remaining, cap) — never more."""
    src = _Source(b'x' * 100)
    r = HTTP1Recipient(src, _conn(100), chunk_max=16)
    await _drain(r)
    asked = [n for n, _ in src.reads]
    assert asked == [16, 16, 16, 16, 16, 16, 4]


@pytest.mark.asyncio
async def test_body_counted_by_returned_not_asked():
    """A short read decrements the body by what arrived, not what was asked."""
    src = _Source(b'abc')
    r = HTTP1Recipient(src, _conn(10), chunk_max=8)
    assert await r.next_chunk() == b'abc'
    assert src.reads[0] == (8, 3)      # asked 8 (capped), got 3
    with pytest.raises(ClientDisconnected):
        await r.next_chunk()


@pytest.mark.asyncio
async def test_truncated_body_is_not_a_complete_body():
    """EOF before the declared length is a truncated upload, never done."""
    src = _Source(b'abc')
    r = HTTP1Recipient(src, _conn(5), chunk_max=8)
    assert await r.next_chunk() == b'abc'
    with pytest.raises(ClientDisconnected):
        await r.next_chunk()


@pytest.mark.asyncio
async def test_zero_cap_falls_back_to_one_byte():
    """A misconfigured 0 cap must not turn every read into EOF (b'')."""
    src = _Source(b'hello')
    r = HTTP1Recipient(src, _conn(5), chunk_max=0)
    assert b''.join(await _drain(r)) == b'hello'


@pytest.mark.asyncio
async def test_cap_larger_than_body_reads_it_in_one():
    src = _Source(b'0123456789')
    r = HTTP1Recipient(src, _conn(10), chunk_max=512 * 1024)
    assert await _drain(r) == [b'0123456789']


class _ZeroLengthTruthy:
    """Zero bytes long, yet truthy — the shape an unscripted async mock returns.

    ``MagicMock(spec=AbstractReader)`` auto-creates ``read`` as an ``AsyncMock``
    because the spec declares it ``async def``, and an ``AsyncMock``'s awaited
    return value is a ``MagicMock``: truthy, with ``__len__`` of 0.  Reproduced
    explicitly here so the test states the property that matters rather than
    depending on ``unittest.mock`` internals.
    """

    def __bool__(self) -> bool:
        return True

    def __len__(self) -> int:
        return 0


class _ZeroLengthSource(AbstractReader):
    """Hands back zero-length reads forever, and fails loudly if asked twice.

    The call cap is the point: without a length-based EOF guard the body
    counter never decrements, so an unbounded ``next_chunk`` loop would spin
    here — and spinning on a mock costs ~7.5 kB per turn, which is how this
    took a VM down.  The cap turns that into an assertion instead.
    """

    def __init__(self, cap: int = 4):
        self.calls = 0
        self._cap = cap

    async def read(self, n: int):
        self.calls += 1
        assert self.calls <= self._cap, (
            f'{self.calls} reads for a body that can never advance — the '
            f'Content-Length loop is not terminating on a zero-length read')
        return _ZeroLengthTruthy()

    async def readexactly(self, n: int) -> bytes:
        raise AssertionError('the Content-Length path must not use readexactly')

    async def readuntil(self, sep: bytes) -> bytes:
        raise AssertionError('unused')


@pytest.mark.asyncio
async def test_zero_length_read_ends_the_body_whatever_its_truthiness():
    """Forward progress is the *length* read, not the truthiness of the result.

    The loop decrements ``_content_length`` by what came back, so a zero-length
    read that is nonetheless truthy would advance the body by nothing and read
    as "more to come" forever.  Zero bytes means the peer is gone, full stop.
    """
    src = _ZeroLengthSource()
    r = HTTP1Recipient(src, _conn(10), chunk_max=8)
    with pytest.raises(ClientDisconnected):
        await r.next_chunk()
    assert src.calls == 1, 'the loop read again after a zero-length read'


@pytest.mark.asyncio
async def test_rebound_recipient_reads_next_body_from_scratch():
    """There is no per-request read-size state to leak into the next request."""
    src = _Source(b'first')
    r = HTTP1Recipient(src, _conn(5), chunk_max=8)
    assert await r.next_chunk() == b'first'
    src.feed(b'second!')
    r.bind(_conn(7))
    assert await _drain(r) == [b'second!']
