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
