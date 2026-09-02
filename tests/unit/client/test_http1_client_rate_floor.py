"""``BB_CLIENT_MIN_BODY_RATE`` — what it measures, and what it refuses.

``BB_CLIENT_BODY_TIMEOUT`` returns on *any* arrival, so it degrades from
"deliver a slice in N seconds" to "send something every N seconds", which a
one-byte drip always satisfies.  A rate is what a drip cannot fake.

Three properties are fixed separately here, because an earlier attempt at this
floor had each of them wrong in a way the others would have hidden:

*The numerator is payload.*  Chunk-size lines, chunk extensions, terminators
and trailers are discarded on receipt, so a peer that pads them buys credit
with octets it never has to make mean anything.  Measured on that attempt, at
identical body rates: 0 B of padding was refused at 0.60 s, and 200 B of
padding survived the whole 15.6 s response.

*The denominator is every read's wait.*  Framing reads keep their seconds even
though their octets do not count — otherwise a peer stalls in front of the
parts that are not counted, and the gap is free.

*The window opens at the first body octet.*  Before it, a peer that flushed
its head and is now working — a slow query, a report built while it is
streamed, an LLM's time to first token — is indistinguishable from one that
is thinking, and both are legitimate.  That attempt refused an 11-byte body
after 8 s of think time while allowing a 3 KiB one, so the verdict turned on
the body's *size*: it was firing on latency, not on rate.

After the first octet no such separation exists.  A gap that eventually
produces a body and one that never does are the same observation until the
next octet arrives, so an event stream and a drip cannot be told apart online.
That is why the floor is **off by default** and why enabling it is a statement
by the operator about their peer — and why each shape below is asserted twice:
completing with the floor off, refused with it on.  Only the pair is a real
gate; "a legitimate slow peer succeeds" alone also passes against a floor that
does nothing.
"""
from __future__ import annotations

import asyncio

import pytest

from blackbull.client.http1 import HTTP1ResponseRecipient
from blackbull.server.recipient import AbstractReader

pytestmark = pytest.mark.asyncio

_RATE = 1000.0
_GRACE = 1.0


class _Clock:
    """A monotonic clock the script moves, so no test waits on real time."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


class _Paced(AbstractReader):
    """Serves ``(gap, data)`` pieces, charging *gap* to the read that takes it.

    A gap is spent inside a read, which is how the floor sees transport wait
    and how it never sees a caller's own time between ``stream()`` yields.
    """

    def __init__(self, clock: _Clock, script: list[tuple[float, bytes]]) -> None:
        self._clock = clock
        self._script = list(script)
        self._buf = b''

    async def read(self, n: int = -1) -> bytes:
        await asyncio.sleep(0)  # a real suspension, so cancellation still works
        while not self._buf:
            if not self._script:
                return b''
            gap, data = self._script.pop(0)
            self._clock.now += gap
            self._buf = data
        want = len(self._buf) if n < 0 else min(n, len(self._buf))
        out, self._buf = self._buf[:want], self._buf[want:]
        return out


def _fixed(pieces: list[tuple[float, bytes]]) -> list[tuple[float, bytes]]:
    total = sum(len(data) for _, data in pieces)
    head = b'HTTP/1.1 200 OK\r\ncontent-length: %d\r\n\r\n' % total
    return [(0.0, head), *pieces]


def _chunked(pieces: list[tuple[float, bytes]], *, ext: bytes = b'',
             last_gap: float = 0.0) -> list[tuple[float, bytes]]:
    """Each piece becomes one whole chunk, so its gap falls on the size line."""
    head = b'HTTP/1.1 200 OK\r\ntransfer-encoding: chunked\r\n\r\n'
    script = [(0.0, head)]
    for gap, data in pieces:
        script.append((gap, b'%x' % len(data) + ext + b'\r\n' + data + b'\r\n'))
    script.append((last_gap, b'0\r\n\r\n'))
    return script


def _steady(count: int, size: int, gap: float) -> list[tuple[float, bytes]]:
    """*count* equal pieces; the first arrives with the head, the rest apart."""
    return [(0.0 if i == 0 else gap, bytes([0x61 + i % 26]) * size)
            for i in range(count)]


def _first_byte_then(count: int, size: int,
                     gap: float) -> list[tuple[float, bytes]]:
    """One byte to open the window, then *count* pieces *gap* apart."""
    return [(0.0, b'x'), *((gap, b'y' * size) for _ in range(count))]


#: Every shape is (script, whether an enabled floor refuses it).
_SHAPES = {
    # 10 B per second against a 1000 B/s floor.
    'drip': (_fixed(_steady(10, 10, 1.0)), True),
    # A legitimate shape the floor cannot distinguish from one: refusing it is
    # the documented cost of enabling the floor, not a defect in the metric.
    'event-stream': (_fixed(_steady(5, 200, 10.0)), True),
    # Head flushed, then 10 s of work, then the whole body.  The window has not
    # opened yet, so this survives an enabled floor.
    'think-then-answer': (_fixed([(10.0, b'x' * 11)]), False),
    # 2 KiB per second, twice the floor: the window rolls and keeps rolling.
    'fast': (_fixed(_steady(6, 2000, 1.0)), False),
    # The same drip, its chunk extension padded 200× the payload.  The padding
    # is discarded on receipt, so it must buy nothing.
    'padded-chunk-ext': (_chunked(_steady(10, 10, 1.0),
                                  ext=b';pad=' + b'x' * 2000), True),
    # The control for it, and a claim of its own: the gap falls on the
    # chunk-size line, so the refusal proves framing reads keep their seconds.
    'stall-before-the-size-line': (_chunked(_steady(10, 10, 1.0)), True),
    # 2 KiB/s, twice the floor, but chunked — so the octets that pay for a
    # framing read's wait arrive in the same delivery and are read on the
    # *next* call.  Judging the framing read alone refuses this peer.
    'chunked-payload-pays-for-its-size-line':
        (_chunked(_first_byte_then(3, 4000, 2.0)), False),
    # The body is delivered fast and then the connection is held for 20 s
    # before the last chunk.  No payload read follows, so this is the case a
    # deferral that never confirms would forgive entirely.
    'stall-before-the-last-chunk':
        (_chunked(_first_byte_then(1, 4000, 0.0), last_gap=20.0), True),
}
_SHAPE_IDS = sorted(_SHAPES)


@pytest.fixture
def clock(monkeypatch):
    dial = _Clock()
    monkeypatch.setattr('blackbull.client.http1._monotonic', dial)
    return dial


def _enable(monkeypatch):
    monkeypatch.setenv('BB_CLIENT_MIN_BODY_RATE', str(_RATE))
    monkeypatch.setenv('BB_CLIENT_MIN_BODY_RATE_GRACE', str(_GRACE))


async def _body_via(path: str, reader) -> bytes:
    """``receive`` and ``stream`` share one meter and are separate public
    paths, so every shape is asserted through both."""
    recipient = HTTP1ResponseRecipient()
    if path == 'receive':
        return (await recipient.receive(reader)).body
    return b''.join([chunk async for chunk in recipient.stream(reader)])


_PATHS = pytest.mark.parametrize('path', ['receive', 'stream'])


@_PATHS
@pytest.mark.parametrize('shape', _SHAPE_IDS)
async def test_off_by_default_every_shape_completes(
        shape, path, clock, monkeypatch):
    """The shipped default refuses none of them, including the drip."""
    script, _ = _SHAPES[shape]
    assert await _body_via(path, _Paced(clock, script))


@_PATHS
@pytest.mark.parametrize('shape', _SHAPE_IDS)
async def test_enabled_refuses_exactly_the_documented_shapes(
        shape, path, clock, monkeypatch):
    """And when it is on, it fires on the rate — not on the size or the wait."""
    _enable(monkeypatch)
    script, refused = _SHAPES[shape]
    read = _body_via(path, _Paced(clock, script))
    if not refused:
        assert await read
        return
    with pytest.raises(TimeoutError) as caught:
        await read
    assert 'BB_CLIENT_MIN_BODY_RATE' in str(caught.value)


class TestTheFloorIsTheOneThatFires:
    """Not the deadline standing in for it."""

    async def test_the_refusal_names_the_rate_not_the_deadline(
            self, clock, monkeypatch):
        _enable(monkeypatch)
        monkeypatch.setenv('BB_CLIENT_BODY_TIMEOUT', '0')  # nothing else can fire
        script, _ = _SHAPES['drip']
        with pytest.raises(TimeoutError, match='BB_CLIENT_MIN_BODY_RATE'):
            await HTTP1ResponseRecipient().receive(_Paced(clock, script))

    async def test_a_refused_response_abandons_the_connection(
            self, clock, monkeypatch):
        """A refusal leaves unread octets, so the reader must not be reused."""
        _enable(monkeypatch)
        script, _ = _SHAPES['drip']
        recipient = HTTP1ResponseRecipient()
        with pytest.raises(TimeoutError):
            await recipient.receive(_Paced(clock, script))
        assert recipient.framing_broken


class TestCallerTimeIsNotPeerTime:
    """A caller slow between ``stream()`` yields must not be read as a slow
    peer: the clock runs inside a read and nowhere else."""

    async def test_slow_consumer_does_not_trip_the_floor(
            self, clock, monkeypatch):
        _enable(monkeypatch)
        script = _fixed(_steady(6, 2000, 0.0))
        body = b''
        async for chunk in HTTP1ResponseRecipient().stream(_Paced(clock, script)):
            clock.now += 60.0  # the caller thinks for a minute per chunk
            body += chunk
        assert len(body) == 12000
