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


import pytest

from blackbull.client.http1 import HTTP1ResponseRecipient

from tests.harness import fed


def _CannedReader(payload: bytes = b''):
    return fed(payload).reader


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
    async def test_zero_disables_each_bound(self, monkeypatch):
        """0 is how every cap in this tree spells "off"."""
        monkeypatch.setenv('BB_CLIENT_HEAD_MAX_TOTAL', '0')
        monkeypatch.setenv('BB_CLIENT_HEAD_MAX_LINE', '0')
        monkeypatch.setenv('BB_CLIENT_HEAD_TIMEOUT', '0')
        fields = [b'x-pad-%03d: %s\r\n' % (i, b'v' * 200) for i in range(40)]
        reader = _CannedReader(_head(*fields) + b'')
        res = await HTTP1ResponseRecipient().receive(reader)
        assert res.status == 200
