"""Adaptive body-read sizing on the HTTP/1.1 ``Content-Length`` path.

BlackBull used to read every body in fixed 64 KiB slices, so an 8.5 MB upload
cost 130 receive round-trips where uvicorn's transport-paced ``receive()``
cost 38.  The slice size now adapts to the peer using Netty's
``AdaptiveRecvByteBufAllocator`` rule: grow while the transport is running
ahead of us, back off only after two consecutive drained reads, clamp to a
ceiling that keeps one read inside ``body_timeout``.

Everything here is asserted through the chunks the recipient hands back — the
sizes *are* the observable, since each one becomes an ``http.request`` event.
The sizing is deliberately free of semantic consequence, and the last two
tests are what pin that: reads stay ``readexactly``, so a truncated body is
still an error and a rebound recipient still starts over.
"""
import pytest

from blackbull.connection import Connection
from blackbull.headers import Headers
from blackbull.request import ClientDisconnected
from blackbull.server.recipient import AbstractReader, HTTP1Recipient


class _Peer(AbstractReader):
    """A reader whose *buffered* backlog is controllable.

    ``ahead_for`` is the number of reads after which the peer stops running
    ahead: up to it the transport always has more waiting (a fast uploader),
    after it every read exactly drains what arrived (a peer keeping pace with
    the server, or slower).  That backlog is the only signal the sizing rule
    consumes, so driving it drives the whole algorithm.
    """

    def __init__(self, data: bytes, *, ahead_for: int = 1 << 30):
        self._d = bytearray(data)
        self._ahead_for = ahead_for
        self.reads = 0

    async def read(self, n: int) -> bytes:
        out = bytes(self._d[:n])
        del self._d[:n]
        return out

    async def readexactly(self, n: int) -> bytes:
        if len(self._d) < n:
            from blackbull.server.recipient import IncompleteReadError
            out, self._d = bytes(self._d), bytearray()
            raise IncompleteReadError(out, n)
        out = bytes(self._d[:n])
        del self._d[:n]
        self.reads += 1
        return out

    def buffered_len(self) -> int:
        return len(self._d) if self.reads <= self._ahead_for else 0


def _conn(length: int) -> Connection:
    return Connection(
        type='http', http_version='1.1', method='POST', path='/upload',
        raw_path=b'/upload', query_string=b'',
        headers=Headers([(b'content-length', str(length).encode())]),
        scheme='http',
    )


async def _drain(r: HTTP1Recipient) -> list[int]:
    """Every chunk the recipient yields, as sizes."""
    sizes = []
    while (chunk := await r.next_chunk()) is not None:
        sizes.append(len(chunk))
    return sizes


@pytest.mark.asyncio
async def test_a_peer_running_ahead_earns_bigger_reads():
    """The upload case: a client whose bytes are already buffered doubles the
    slice each read, so the body costs a handful of round-trips instead of
    ``length / chunk_size`` of them."""
    body = bytes(200)
    r = HTTP1Recipient(_Peer(body), _conn(200), chunk_size=8, chunk_max=64)

    sizes = await _drain(r)

    assert sizes == [8, 16, 32, 64, 64, 16]
    assert sum(sizes) == 200          # the whole body, still exactly once
    assert len(sizes) < 200 // 8      # 6 reads, not 25


@pytest.mark.asyncio
async def test_growth_stops_at_the_ceiling():
    """The cap is a latency bound, not a memory one — no read may ever promise
    more than ``body_timeout`` can deliver, however fast the peer looks."""
    r = HTTP1Recipient(_Peer(bytes(500)), _conn(500), chunk_size=8, chunk_max=32)

    sizes = await _drain(r)

    assert max(sizes) == 32
    assert sizes[:5] == [8, 16, 32, 32, 32]


@pytest.mark.asyncio
async def test_a_peer_that_never_runs_ahead_stays_at_the_starting_size():
    """A slow uploader must not be talked into a read it cannot fill in time.
    With no backlog there is no evidence to grow on, so nothing grows."""
    r = HTTP1Recipient(_Peer(bytes(40), ahead_for=0), _conn(40),
                       chunk_size=8, chunk_max=512)

    assert await _drain(r) == [8, 8, 8, 8, 8]


@pytest.mark.asyncio
async def test_backing_off_takes_two_drained_reads_and_floors_at_the_base():
    """Netty's two-stage shrink: one drained read is indistinguishable from a
    peer keeping exact pace or pausing for a moment, so it only arms the
    back-off.  Each size therefore appears twice on the way down, and the
    descent stops at the starting size rather than collapsing toward zero."""
    r = HTTP1Recipient(_Peer(bytes(400), ahead_for=3), _conn(400),
                       chunk_size=8, chunk_max=64)

    sizes = await _drain(r)

    assert sizes[:11] == [8, 16, 32, 64,   # earned while the peer was ahead
                          64, 32,          # first drained read only arms
                          32, 16,          # …and so does each one after
                          16, 8, 8]
    assert min(sizes) == 8
    assert sum(sizes) == 400


@pytest.mark.asyncio
async def test_a_ceiling_below_the_base_pins_the_old_fixed_size_behaviour():
    """``BB_BODY_CHUNK_MAX`` == ``BB_BODY_CHUNK_SIZE`` is the documented way
    back to fixed slices; a ceiling *under* the base must not invert into a
    first read that is also the last."""
    r = HTTP1Recipient(_Peer(bytes(64)), _conn(64), chunk_size=16, chunk_max=4)

    assert await _drain(r) == [16, 16, 16, 16]


@pytest.mark.asyncio
async def test_a_truncated_body_is_still_an_error_at_any_read_size():
    """The reason the sizing needed no design decision: reads stay
    ``readexactly``, so a peer that promises 200 bytes and sends 100 still
    fails instead of reading as a complete upload."""
    r = HTTP1Recipient(_Peer(bytes(100)), _conn(200), chunk_size=8, chunk_max=64)

    with pytest.raises(ClientDisconnected):
        await _drain(r)


@pytest.mark.asyncio
async def test_a_rebound_recipient_does_not_inherit_the_previous_read_size():
    """Sizing is per request, not per connection.  Request N's fast peer says
    nothing about request N+1, which on a keep-alive connection may be a
    different upload entirely — and is served by the same recipient."""
    peer = _Peer(bytes(200))
    r = HTTP1Recipient(peer, _conn(200), chunk_size=8, chunk_max=64)
    await _drain(r)                        # grows to the 64-byte ceiling

    peer._d = bytearray(bytes(40))         # next request on the connection
    peer.reads = 0
    r.bind(_conn(40))

    assert (await _drain(r))[0] == 8       # starts over, not at 64


@pytest.mark.asyncio
async def test_a_single_read_request_between_two_uploads_carries_nothing_across():
    """Only a body that could take more than one read re-arms the sizing, so a
    small request in between skips that work.  It must not become a hole the
    first upload's size escapes through: the small body is served from its own
    length, and the second upload still starts from scratch."""
    peer = _Peer(bytes(200))
    r = HTTP1Recipient(peer, _conn(200), chunk_size=8, chunk_max=64)
    await _drain(r)                        # grows to the 64-byte ceiling

    peer._d = bytearray(bytes(5))          # a small request rides in between
    peer.reads = 0
    r.bind(_conn(5))
    assert await _drain(r) == [5]          # its own length, not the stale 64

    peer._d = bytearray(bytes(200))        # …and the next upload starts over
    peer.reads = 0
    r.bind(_conn(200))
    assert (await _drain(r))[0] == 8


@pytest.mark.asyncio
async def test_a_connection_whose_first_request_is_small_still_reads_it():
    """The sizing fields are bound once per connection rather than per request,
    so a connection that opens with a bodyless or tiny request must not find
    them missing."""
    r = HTTP1Recipient(_Peer(bytes(3)), _conn(3), chunk_size=8, chunk_max=64)

    assert await _drain(r) == [3]
