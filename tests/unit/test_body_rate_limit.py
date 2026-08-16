"""Slow-drip defence — a minimum body-delivery rate, on both protocols.

Transport-paced reads cost us the per-slice deadline.  ``readexactly(64 KiB)``
under ``body_timeout`` meant "fill a slice in 30 s or be cut"; an up-to-n read
returns on *any* arrival, so the same deadline now only asks the peer to send
*something* every 30 s — which one byte satisfies, indefinitely.  A minimum
*rate* is the thing a drip cannot fake, and it is what Kestrel
(``MinRequestBodyDataRate``), nginx, Node and actix all reach for.

What the rate is measured against is the whole design, and it differs by
protocol for a reason:

* **HTTP/1.1** — only time spent *waiting on the transport* counts.  The body
  arrives when we read it, so a handler that spends a second writing each chunk
  to disk would otherwise look exactly like a peer that spent a second sending
  it.
* **HTTP/2** — wall clock from the first DATA frame, because DATA lands whether
  or not the handler is reading.  The exemption there is different: a peer
  back-pressured by our own closed inbound window is not judged at all.

The clock is injected rather than slept through — a test that proves a 5 s
grace period by waiting 5 s is a test nobody runs.
"""
from http import HTTPStatus

import pytest

from blackbull.connection import Connection
from blackbull.headers import Headers
from blackbull.protocol.frame_types import Data, DataFrameFlags, FrameTypes
from blackbull.request import ClientDisconnected
from blackbull.router import HTTPException
from blackbull.server.recipient import (
    AbstractReader, HTTP1Recipient, HTTP2Recipient,
)

pytestmark = pytest.mark.asyncio


class _Clock:
    """A monotonic clock the test drives, in seconds."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def clock(monkeypatch):
    c = _Clock()
    monkeypatch.setattr('blackbull.server.recipient._monotonic', c)
    return c


# ---------------------------------------------------------------------------
# HTTP/1.1
# ---------------------------------------------------------------------------

class _PacedSource(AbstractReader):
    """Hands back *per_read* bytes, costing *seconds* of transport wait each.

    The cost is charged inside ``read`` because that is where the recipient's
    clock runs — which is the property under test, not an implementation
    detail: time spent anywhere else is not the peer's.
    """

    def __init__(self, clock: _Clock, seconds: float, per_read: int) -> None:
        self._clock = clock
        self._seconds = seconds
        self._per_read = per_read
        self.reads = 0

    async def read(self, n: int) -> bytes:
        self.reads += 1
        self._clock.advance(self._seconds)
        return b'x' * min(n, self._per_read)


class _BurstThenDrip(AbstractReader):
    """Delivers *burst* octets at ~zero wait, then *per_read* bytes every
    *seconds* of transport wait — the shape a cumulative rate judge cannot
    see, and the reason the windowed one exists."""

    def __init__(self, clock: _Clock, burst: int, seconds: float,
                 per_read: int) -> None:
        self._clock = clock
        self._burst = burst
        self._seconds = seconds
        self._per_read = per_read
        self._sent = 0
        self.reads = 0

    async def read(self, n: int) -> bytes:
        self.reads += 1
        if self._sent < self._burst:
            out = min(n, self._burst - self._sent)
            self._sent += out
            return b'x' * out
        self._clock.advance(self._seconds)
        return b'y' * self._per_read


def _conn(body_len: int) -> Connection:
    return Connection(
        method='POST', path='/p', raw_path=b'/p',
        headers=Headers([(b'content-length', str(body_len).encode())]),
        type='http')


def _recipient(src, *, rate=240.0, grace=5.0, body=1_000_000) -> HTTP1Recipient:
    return HTTP1Recipient(src, _conn(body), chunk_size=64 * 1024,
                          chunk_max=64 * 1024, max_body=0,
                          min_rate=rate, min_rate_grace=grace)


class TestHTTP1TheRateIsJudgedOnWaitingTime:
    async def test_a_trickle_is_cut_once_the_grace_period_is_spent(self, clock):
        """One byte per two seconds: never silent, never enough."""
        src = _PacedSource(clock, seconds=2.0, per_read=1)
        r = _recipient(src)
        with pytest.raises(ClientDisconnected):
            while await r.next_chunk() is not None:
                pass
        # 2 s per byte, 5 s of grace: the third read is the first one whose
        # accumulated wait (6 s) is past it, so that is where the verdict lands.
        assert src.reads == 3, (
            f'the drip ran for {src.reads} reads before the rate was judged')

    async def test_the_grace_period_is_honoured(self, clock):
        """Nothing is judged on its first packets — the check only starts once
        the connection has spent more than the grace period waiting."""
        src = _PacedSource(clock, seconds=1.0, per_read=1)
        r = _recipient(src, grace=5.0)
        for _ in range(5):
            assert await r.next_chunk() == b'x'   # 5 s of waiting, still inside
        with pytest.raises(ClientDisconnected):
            await r.next_chunk()

    async def test_a_fast_peer_is_never_judged(self, clock):
        """A real upload spends almost no time waiting, so the rate never even
        gets a denominator worth dividing by."""
        src = _PacedSource(clock, seconds=0.001, per_read=64 * 1024)
        r = _recipient(src, body=64 * 1024 * 100)
        for _ in range(100):
            assert len(await r.next_chunk()) == 64 * 1024

    async def test_a_slow_handler_is_not_mistaken_for_a_slow_peer(self, clock):
        """The false positive worth designing against.

        A handler that spends ten seconds on each chunk it receives makes the
        request take just as long as a drip does.  The difference — the only
        difference — is who spent the time, so the clock only runs while the
        recipient is waiting on the transport.
        """
        src = _PacedSource(clock, seconds=0.001, per_read=1024)
        r = _recipient(src, body=1024 * 10)
        for _ in range(10):
            assert len(await r.next_chunk()) == 1024
            clock.advance(10.0)        # the handler's own work, not the peer's

    async def test_zero_rate_disables_the_detector(self, clock):
        src = _PacedSource(clock, seconds=60.0, per_read=1)
        r = _recipient(src, rate=0.0)
        for _ in range(5):
            assert await r.next_chunk() == b'x'

    async def test_the_refusal_closes_the_connection(self, clock):
        """A peer that is being dropped for dripping must not be handed a fresh
        keep-alive slot to drip into."""
        src = _PacedSource(clock, seconds=2.0, per_read=1)
        r = _recipient(src)
        with pytest.raises(ClientDisconnected):
            while await r.next_chunk() is not None:
                pass
        assert r.must_close is True
        assert r.needs_drain() is False

    async def test_a_burst_does_not_shelter_a_subsequent_stall(self, clock):
        """1 MiB at full speed, then one byte every two seconds.  A cumulative
        judge would coast on the burst — the lifetime average stays above
        240 B/s for ~73 minutes.  The windowed judge rolls the burst's window
        and cuts the stall after one more grace period."""
        src = _BurstThenDrip(clock, burst=1024 * 1024, seconds=2.0, per_read=1)
        r = _recipient(src, body=10 * 1024 * 1024)
        with pytest.raises(ClientDisconnected):
            while await r.next_chunk() is not None:
                pass
        # 16 burst reads (64 KiB each) + 3 drips in the burst's window + 3 in
        # the next: the verdict lands around two grace periods in, not 73 min.
        assert src.reads < 50
        assert clock.now < 60.0


# ---------------------------------------------------------------------------
# HTTP/2
# ---------------------------------------------------------------------------

def _data(payload: bytes, *, end_stream: bool = False, stream_id: int = 1) -> Data:
    flags = DataFrameFlags.END_STREAM if end_stream else 0
    return Data(len(payload), FrameTypes.DATA, flags, stream_id, data=payload)


class TestHTTP2BodyLimitsAreJudgedOnArrival:
    """DATA lands whether or not the handler reads, so the queue grows on the
    peer's schedule — which is why both limits are answered at arrival."""

    async def test_an_undeclared_body_over_the_cap_is_refused(self, clock):
        # ``content-length`` would have been refused at HEADERS with a 413;
        # this is the body that declared nothing.
        r = HTTP2Recipient(max_body=1024, min_rate=0.0)
        assert r.put_DATAFrame(_data(b'a' * 700)) is True
        assert r.put_DATAFrame(_data(b'b' * 700)) is False

    async def test_a_body_within_the_cap_is_delivered(self, clock):
        r = HTTP2Recipient(max_body=1024, min_rate=0.0)
        assert r.put_DATAFrame(_data(b'a' * 512)) is True
        assert r.put_DATAFrame(_data(b'b' * 512, end_stream=True)) is True
        assert await r.next_chunk() == b'a' * 512
        assert await r.next_chunk() == b'b' * 512

    async def test_a_trickled_stream_is_refused_past_the_grace(self, clock):
        r = HTTP2Recipient(max_body=0, min_rate=240.0, min_rate_grace=5.0)
        assert r.put_DATAFrame(_data(b'a')) is True     # clock starts here
        clock.advance(3.0)
        assert r.put_DATAFrame(_data(b'a')) is True     # inside the grace
        clock.advance(3.0)
        assert r.put_DATAFrame(_data(b'a')) is False    # 3 bytes in 6 s

    async def test_a_peer_we_back_pressured_is_not_judged(self, clock):
        """The HTTP/2-specific false positive: a slow *handler* stops crediting,
        the inbound window closes, and the peer stops sending because we told it
        to.  Judging its rate then would reset the stream for our own doing.
        """
        async def _credit(_n):        # consume-time crediting, never replayed
            pass

        r = HTTP2Recipient(credit_callback=_credit, credit_budget=1024,
                           max_body=0, min_rate=240.0, min_rate_grace=5.0)
        assert r.put_DATAFrame(_data(b'a' * 512)) is True   # clock starts here
        assert r.put_DATAFrame(_data(b'a' * 512)) is True   # window now closed
        clock.advance(60.0)
        # The peer could not send during that minute — its window was shut
        # until the handler finally popped a frame and credit was replayed.
        assert await r.next_chunk() == b'a' * 512
        assert r.put_DATAFrame(_data(b'a')) is True

    async def test_zero_rate_disables_the_detector(self, clock):
        r = HTTP2Recipient(max_body=0, min_rate=0.0)
        assert r.put_DATAFrame(_data(b'a')) is True
        clock.advance(3600.0)
        assert r.put_DATAFrame(_data(b'a')) is True

    async def test_a_burst_does_not_shelter_a_subsequent_stall(self, clock):
        """1 MiB at full speed, then one byte at a time.  The windowed judge
        rolls the burst's window and cuts the stall in the next one; a
        cumulative judge would coast for ~73 minutes."""
        r = HTTP2Recipient(max_body=0, min_rate=240.0, min_rate_grace=5.0)
        payload = b'x' * (64 * 1024)
        for _ in range(16):                    # 1 MiB at full speed
            assert r.put_DATAFrame(_data(payload)) is True
        clock.advance(3.0)
        assert r.put_DATAFrame(_data(b'y')) is True     # burst's window rolls
        clock.advance(3.0)
        assert r.put_DATAFrame(_data(b'y')) is True     # next window, inside grace
        clock.advance(3.0)
        assert r.put_DATAFrame(_data(b'y')) is True
        clock.advance(3.0)
        assert r.put_DATAFrame(_data(b'y')) is False    # that window has only drips


class TestTheTwoProtocolsAgreeOnTheStatus:
    async def test_http1_answers_413_for_an_over_cap_body(self):
        """Both protocols refuse the same octets; only the *shape* of the
        refusal differs (HTTP/2 resets the stream, HTTP/1.1 cannot).  The
        HTTP/1.1 answer is the one that carries a status."""
        class _Big(AbstractReader):
            async def read(self, n: int) -> bytes:
                return b'x' * n

        r = HTTP1Recipient(_Big(), _conn(10_000), chunk_size=64 * 1024,
                           chunk_max=4096, max_body=1024, min_rate=0.0)
        with pytest.raises(HTTPException) as exc:
            await r.next_chunk()
        assert exc.value.status == HTTPStatus.REQUEST_ENTITY_TOO_LARGE
