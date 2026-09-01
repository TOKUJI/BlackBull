"""A peer that trickles is abandoned; a peer that is merely slow is not.

``BB_CLIENT_BODY_TIMEOUT`` bounds one read, so it stops a peer that **stops**.
It cannot stop one that sends a byte just before each deadline: every
individual read succeeds and the response never ends.  A rate is what a drip
cannot fake, which is the same reasoning ``BB_MIN_BODY_RATE`` carries on the
server.

The window is one grace period wide and rolls, so a burst that ran ahead buys
one window rather than the whole response — a peer cannot deliver a megabyte
and then stall indefinitely on the strength of its average.
"""
from __future__ import annotations

import asyncio

import pytest

from blackbull.client.http1 import HTTP1ResponseRecipient
from blackbull.server.rate_window import ByteRateFloor
from blackbull.server.recipient import AbstractReader


class _Trickle(AbstractReader):
    """Delivers *per_read* octets every *interval* seconds, forever."""

    def __init__(self, head: bytes, per_read: int, interval: float) -> None:
        self._head, self._pos = head, 0
        self._per_read, self._interval = per_read, interval

    async def read(self, n: int = -1) -> bytes:
        if self._pos < len(self._head):
            out = self._head[self._pos:] if n < 0 else self._head[self._pos:self._pos + n]
            self._pos += len(out)
            return out
        await asyncio.sleep(self._interval)
        want = self._per_read if n < 0 else min(n, self._per_read)
        return b'x' * want


_HEAD = b'HTTP/1.1 200 OK\r\ncontent-length: 1000000\r\n\r\n'


class TestByteRateFloor:
    """The primitive, driven directly — an injected clock beats sleeping."""

    def test_nothing_is_judged_inside_the_grace_period(self):
        floor = ByteRateFloor(rate=1000.0, grace=1.0)
        assert floor.record(0, waited=0.9) is False

    def test_a_window_below_the_floor_fails(self):
        floor = ByteRateFloor(rate=1000.0, grace=1.0)
        assert floor.record(10, waited=1.5) is True

    def test_a_window_above_the_floor_rolls_forward(self):
        floor = ByteRateFloor(rate=1000.0, grace=1.0)
        assert floor.record(5000, waited=1.5) is False
        # Rolled: the next judgement starts from a clean slate, so the burst
        # above cannot shelter the stall below.
        assert floor.record(10, waited=1.5) is True

    def test_zero_disables_it(self):
        floor = ByteRateFloor(rate=0.0, grace=1.0)
        assert floor.record(0, waited=100.0) is False


class TestClientRateFloor:
    @pytest.mark.asyncio
    async def test_a_trickling_peer_is_abandoned(self, monkeypatch):
        """One octet per read, comfortably inside every per-read deadline."""
        monkeypatch.setenv('BB_CLIENT_BODY_TIMEOUT', '5')
        monkeypatch.setenv('BB_CLIENT_MIN_BODY_RATE', '1000')
        monkeypatch.setenv('BB_CLIENT_MIN_BODY_RATE_GRACE', '0.1')
        reader = _Trickle(_HEAD, per_read=1, interval=0.02)
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(
                HTTP1ResponseRecipient().receive(reader), 3.0)

    @pytest.mark.asyncio
    async def test_a_peer_keeping_up_is_left_alone(self, monkeypatch):
        monkeypatch.setenv('BB_CLIENT_MIN_BODY_RATE', '1000')
        monkeypatch.setenv('BB_CLIENT_MIN_BODY_RATE_GRACE', '0.1')
        wire = (b'HTTP/1.1 200 OK\r\ncontent-length: 6\r\n\r\nhello!')

        class _Prompt(AbstractReader):
            def __init__(self): self._b, self._i = wire, 0
            async def read(self, n: int = -1) -> bytes:
                out = self._b[self._i:] if n < 0 else self._b[self._i:self._i + n]
                self._i += len(out)
                return out

        res = await HTTP1ResponseRecipient().receive(_Prompt())
        assert res.body == b'hello!'

    @pytest.mark.asyncio
    async def test_zero_disables_the_floor(self, monkeypatch):
        monkeypatch.setenv('BB_CLIENT_MIN_BODY_RATE', '0')
        monkeypatch.setenv('BB_CLIENT_BODY_TIMEOUT', '0.2')
        reader = _Trickle(_HEAD, per_read=1, interval=0.02)
        # Still bounded by nothing but the caller's own patience, so the outer
        # guard is what ends it — and it must not be the floor.
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(
                HTTP1ResponseRecipient().receive(reader), 0.8)
