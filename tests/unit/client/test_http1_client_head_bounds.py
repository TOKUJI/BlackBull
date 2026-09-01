"""The client's response head is bounded in all three columns.

``_read_start`` read the status line and every field line with an unbounded
``readuntil``, so a peer could hold the connection open and grow the client's
memory with one endless header — the mirror image of what the server refuses
with 431.  The three bounds are the client's own, because the numbers a client
should accept from a chosen peer are not the numbers a server should accept
from anyone.

Enforcement mirrors the server rather than inventing a shape: the **total**
bounds accumulation during the read (``read_head``), and the **unit** is a
walk over the lines of an already-bounded head — the server does exactly this
in ``HTTP1Actor._parse``, because a line can never be longer than the block
holding it, so the per-line rule is a policy check and not a second memory
guard.
"""
from __future__ import annotations

import asyncio

import pytest

from blackbull.client.exceptions import ResponseTooLarge
from blackbull.client.http1 import HTTP1ResponseRecipient
from blackbull.server.recipient import AbstractReader


class _CannedReader(AbstractReader):
    """Plays back a fixed byte stream, then EOF."""

    def __init__(self, payload: bytes) -> None:
        self._buf = payload
        self._pos = 0

    async def read(self, n: int = -1) -> bytes:
        if n < 0:
            out = self._buf[self._pos:]
            self._pos = len(self._buf)
            return out
        out = self._buf[self._pos:self._pos + n]
        self._pos += len(out)
        return out


class _StallingReader(AbstractReader):
    """Delivers a partial head and then never speaks again."""

    def __init__(self, prefix: bytes) -> None:
        self._prefix = prefix
        self._pos = 0

    async def read(self, n: int = -1) -> bytes:
        if self._pos < len(self._prefix):
            out = self._prefix[self._pos:self._pos + (len(self._prefix) if n < 0 else n)]
            self._pos += len(out)
            return out
        await asyncio.Event().wait()          # a peer that stopped mid-head
        raise AssertionError('unreachable')


def _head(*fields: bytes) -> bytes:
    return b'HTTP/1.1 200 OK\r\n' + b''.join(fields) + b'\r\n'


class TestHeadBounds:
    @pytest.mark.asyncio
    async def test_a_normal_response_still_parses(self):
        reader = _CannedReader(_head(b'content-length: 2\r\n') + b'hi')
        res = await HTTP1ResponseRecipient().receive(reader)
        assert res.status == 200
        assert res.body == b'hi'

    @pytest.mark.asyncio
    async def test_a_head_over_the_total_budget_is_refused(self, monkeypatch):
        monkeypatch.setenv('BB_CLIENT_HEAD_MAX_TOTAL', '512')
        fields = [b'x-pad-%03d: %s\r\n' % (i, b'v' * 40) for i in range(40)]
        reader = _CannedReader(_head(*fields))
        with pytest.raises(ResponseTooLarge):
            await HTTP1ResponseRecipient().receive(reader)

    @pytest.mark.asyncio
    async def test_one_oversized_line_is_refused_inside_the_total(self, monkeypatch):
        """The unit column is not the total: this head fits, one line does not."""
        monkeypatch.setenv('BB_CLIENT_HEAD_MAX_TOTAL', '8192')
        monkeypatch.setenv('BB_CLIENT_HEAD_MAX_LINE', '64')
        reader = _CannedReader(_head(b'x-big: ' + b'v' * 200 + b'\r\n'))
        with pytest.raises(ResponseTooLarge):
            await HTTP1ResponseRecipient().receive(reader)

    @pytest.mark.asyncio
    async def test_a_stalled_head_times_out_rather_than_hanging(self, monkeypatch):
        """A bare ``TimeoutError``, matching ``_connect``'s choice: a peer that
        stalled reads differently from one that answered and refused."""
        monkeypatch.setenv('BB_CLIENT_HEAD_TIMEOUT', '0.05')
        reader = _StallingReader(b'HTTP/1.1 200 OK\r\nx-half: ')
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(HTTP1ResponseRecipient().receive(reader), 2.0)

    @pytest.mark.asyncio
    async def test_zero_disables_each_bound(self, monkeypatch):
        """0 is how every cap in this tree spells "off"."""
        monkeypatch.setenv('BB_CLIENT_HEAD_MAX_TOTAL', '0')
        monkeypatch.setenv('BB_CLIENT_HEAD_MAX_LINE', '0')
        monkeypatch.setenv('BB_CLIENT_HEAD_TIMEOUT', '0')
        fields = [b'x-pad-%03d: %s\r\n' % (i, b'v' * 200) for i in range(40)]
        reader = _CannedReader(_head(*fields) + b'')
        res = await HTTP1ResponseRecipient().receive(reader)
        assert res.status == 200
