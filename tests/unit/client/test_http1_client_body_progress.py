"""``BB_CLIENT_BODY_TIMEOUT`` bounds one read, so a body that keeps arriving
must never hit it however long it takes in total.

The knob is documented — in ``env.py`` and in the reference — as a per-read
progress deadline, the same shape as the server's ``BB_BODY_TIMEOUT`` and
nginx's ``client_body_timeout``.  Three paths did not implement that.
``readexactly`` loops internally until its slice is full, so every bound above
it sees a single read: the buffering ``Content-Length`` path asked for the
whole body in one call, and both sliced paths asked for up to 64 KiB in one
call.  A peer delivering steadily in pieces smaller than that was therefore
refused for outlasting a deadline it never once stopped making progress
against.

Each test here is paired with its control: the same reader with one gap
longer than the deadline must still be refused.  Without the pair, "a slow
body succeeds" also passes against a client carrying no deadline at all.
"""
from __future__ import annotations

import asyncio

import pytest

from blackbull.client.http1 import HTTP1ResponseRecipient
from blackbull.server.recipient import AbstractReader

pytestmark = pytest.mark.asyncio

#: One read's deadline for every test in this file.
_TIMEOUT = 0.3
#: Delivery gap: an order of magnitude inside the deadline, and small enough
#: that the body as a whole still takes twice the deadline to arrive.
_GAP = 0.02
_SLICE = 100
_BODY = b'x' * 3000


class _Dribbler(AbstractReader):
    """Delivers *payload* in ``_SLICE``-sized pieces, ``_GAP`` apart.

    *stall_at* is a byte offset at which one gap exceeds the deadline — the
    control that keeps these tests from passing against no deadline at all.
    It is an offset rather than a read count because the head is read line by
    line, so which read lands in the body is not something the test can count.
    """

    def __init__(self, payload: bytes, stall_at: int | None = None) -> None:
        self._buf, self._pos = payload, 0
        self._stall_at = stall_at

    async def read(self, n: int = -1) -> bytes:
        if self._pos >= len(self._buf):
            return b''
        if self._stall_at is not None and self._pos >= self._stall_at:
            self._stall_at = None
            await asyncio.sleep(_TIMEOUT * 4)
        await asyncio.sleep(_GAP)
        want = _SLICE if n < 0 else min(n, _SLICE)
        out = self._buf[self._pos:self._pos + want]
        self._pos += len(out)
        return out


def _fixed(body: bytes) -> bytes:
    return (b'HTTP/1.1 200 OK\r\ncontent-length: %d\r\n\r\n' % len(body)) + body


def _chunked(body: bytes) -> bytes:
    return (b'HTTP/1.1 200 OK\r\ntransfer-encoding: chunked\r\n\r\n'
            + b'%x\r\n' % len(body) + body + b'\r\n0\r\n\r\n')


#: Well past any head these tests build, so the long gap always lands in the
#: body — where the body deadline, not the head deadline, is the one on watch.
_MID_BODY = 500

_WIRES = pytest.mark.parametrize('wire', [_fixed, _chunked],
                                 ids=['fixed', 'chunked'])


@pytest.fixture(autouse=True)
def _short_deadline(monkeypatch):
    monkeypatch.setenv('BB_CLIENT_BODY_TIMEOUT', str(_TIMEOUT))


async def _streamed(reader) -> bytes:
    return b''.join([chunk async for chunk
                     in HTTP1ResponseRecipient().stream(reader)])


class TestSteadyDeliveryOutlivesTheDeadline:
    """The body takes ~0.6 s against a 0.3 s deadline and must still arrive."""

    @_WIRES
    async def test_receive_reads_the_whole_body(self, wire):
        response = await HTTP1ResponseRecipient().receive(_Dribbler(wire(_BODY)))
        assert response.body == _BODY

    @_WIRES
    async def test_stream_reads_the_whole_body(self, wire):
        assert await _streamed(_Dribbler(wire(_BODY))) == _BODY


class TestOneLongGapIsStillRefused:
    """The control: a deadline that cannot fire would pass the tests above."""

    @_WIRES
    async def test_receive_refuses_a_stalled_peer(self, wire):
        with pytest.raises(TimeoutError):
            await HTTP1ResponseRecipient().receive(
                _Dribbler(wire(_BODY), stall_at=_MID_BODY))

    @_WIRES
    async def test_stream_refuses_a_stalled_peer(self, wire):
        with pytest.raises(TimeoutError):
            await _streamed(_Dribbler(wire(_BODY), stall_at=_MID_BODY))


# ----------------------------------------------------------------------
# What "one read" means for the framing operations
# ----------------------------------------------------------------------

_CHUNKED_HEAD = b'HTTP/1.1 200 OK\r\ntransfer-encoding: chunked\r\n\r\n'


class _Scripted(AbstractReader):
    """Delivers ``(gap, data)`` arrivals exactly as scripted.

    ``_Dribbler`` above slices the wire for the test; this one lets a test
    place a TCP segment boundary in the middle of a chosen operation, which is
    what distinguishes an operation's deadline from an arrival's.
    """

    def __init__(self, script: list[tuple[float, bytes]]) -> None:
        self._script, self._buf = list(script), b''

    async def read(self, n: int = -1) -> bytes:
        while not self._buf:
            if not self._script:
                return b''
            gap, data = self._script.pop(0)
            await asyncio.sleep(gap)
            self._buf = data
        want = len(self._buf) if n < 0 else min(n, len(self._buf))
        out, self._buf = self._buf[:want], self._buf[want:]
        return out


#: Each entry splits exactly one operation across four arrivals, each gap
#: comfortably inside the deadline while the operation as a whole is not.
#:
#: The payload row is the contrast the other three exist against.  A payload
#: read is transport-paced, so it returns on each arrival and the deadline is
#: per arrival.  A framing operation is *not* split: the chunk-size line, a
#: trailer field line and the two-byte terminator are each read whole, and the
#: deadline covers the operation.
#:
#: That is deliberate, not an oversight of the transport-pacing fix.  The
#: reason a payload read had to be paced is that its unit was unbounded — one
#: ``readexactly`` covered a whole body — so the deadline covered unbounded
#: work.  A framing line is bounded by ``BB_CLIENT_HEAD_MAX_LINE`` (8 KiB) and
#: is normally five bytes, and the terminator is two.  Pacing them would trade
#: a bound that owns the operation's *total* time for one that owns a single
#: arrival, leaving the total unowned whenever the rate floor is off — which is
#: its default.  A peer dribbling an 8 KiB chunk-size line one byte per 29 s
#: would then hold the connection for 66 hours.  The response head is bounded
#: the same way and for the same reason: one ``BB_CLIENT_HEAD_TIMEOUT`` covers
#: the whole 64 KiB block, not each line and not each arrival.
_SPLIT_OPERATIONS = {
    'payload': ([(0.0, _CHUNKED_HEAD), (0.0, b'8\r\n'),
                 (_GAP4 := _TIMEOUT / 3, b'ab'), (_GAP4, b'cd'),
                 (_GAP4, b'ef'), (_GAP4, b'gh'),
                 (0.0, b'\r\n0\r\n\r\n')], b'abcdefgh'),
    'chunk-size line': ([(0.0, _CHUNKED_HEAD),
                         (_GAP4, b'8'), (_GAP4, b';x'), (_GAP4, b'=y'),
                         (_GAP4, b'\r\n'),
                         (0.0, b'abcdefgh\r\n0\r\n\r\n')], None),
    'trailer line': ([(0.0, _CHUNKED_HEAD), (0.0, b'2\r\nhi\r\n'), (0.0, b'0\r\n'),
                      (_GAP4, b'x:'), (_GAP4, b' y'), (_GAP4, b'z'),
                      (_GAP4, b'\r\n'), (0.0, b'\r\n')], None),
    'chunk terminator': ([(0.0, _CHUNKED_HEAD), (0.0, b'2\r\nhi'),
                          (_TIMEOUT * 0.7, b'\r'), (_TIMEOUT * 0.7, b'\n'),
                          (0.0, b'0\r\n\r\n')], None),
}

_OPERATIONS = pytest.mark.parametrize(
    'operation', sorted(_SPLIT_OPERATIONS), ids=sorted(_SPLIT_OPERATIONS))
_PATHS = pytest.mark.parametrize('path', ['receive', 'stream'])


async def _body_via(path: str, reader) -> bytes:
    recipient = HTTP1ResponseRecipient()
    if path == 'receive':
        return (await recipient.receive(reader)).body
    return b''.join([chunk async for chunk in recipient.stream(reader)])


@_PATHS
@_OPERATIONS
async def test_the_deadlines_unit_is_the_operation(operation, path):
    """A payload read is paced by arrival; a framing operation is not."""
    script, expected = _SPLIT_OPERATIONS[operation]
    read = _body_via(path, _Scripted(script))
    if expected is not None:
        assert await read == expected
        return
    with pytest.raises(TimeoutError):
        await read


@_PATHS
async def test_operations_do_not_share_one_budget(path):
    """Ten chunks, each a third of a deadline apart — the response takes several
    deadlines in total and must still complete.  This is the half of "the peer
    must keep making progress" that does hold, and the half a single
    whole-response deadline would break."""
    script = [(0.0, _CHUNKED_HEAD)]
    script += [(_TIMEOUT / 3, b'4\r\nabcd\r\n') for _ in range(10)]
    script += [(_TIMEOUT / 3, b'0\r\n\r\n')]
    assert await _body_via(path, _Scripted(script)) == b'abcd' * 10
