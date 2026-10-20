"""``BB_MAX_CONNECTIONS``: a connection past the cap gets a well-formed 503.

A silent reset would leave the client unable to tell overload from failure;
``Retry-After`` and ``connection: close`` tell it to come back on a new
connection.
"""
from __future__ import annotations

import asyncio
import logging

import pytest

from tests.harness import served

pytestmark = pytest.mark.asyncio

REQUEST = b'GET / HTTP/1.1\r\nHost: x\r\n\r\n'


async def _app(scope, receive, send):
    await send({'type': 'http.response.start', 'status': 200, 'headers': []})
    await send({'type': 'http.response.body', 'body': b'ok'})


async def _held(s):
    """A connection the server has accepted and is serving."""
    held = await s.wire()
    await held.send(REQUEST)
    assert (await held.response()).status == 200
    return held


async def test_past_the_cap_is_a_complete_503(caplog):
    caplog.set_level(logging.WARNING, logger='blackbull.caps')
    async with served(_app, max_connections=1) as s:
        await _held(s)
        payload = await (await s.wire()).recv_all()

    assert payload == (b'HTTP/1.1 503 Service Unavailable\r\n'
                       b'retry-after: 1\r\n'
                       b'content-length: 0\r\n'
                       b'connection: close\r\n'
                       b'\r\n')
    hits = [r for r in caplog.records if getattr(r, 'cap', None) == 'max_connections']
    assert hits and hits[0].levelno == logging.WARNING and hits[0].limit == 1


async def test_below_the_cap_no_cap_hit_is_logged(caplog):
    caplog.set_level(logging.WARNING, logger='blackbull.caps')
    async with served(_app, max_connections=2) as s:
        await _held(s)
        await _held(s)
    assert not [r for r in caplog.records if getattr(r, 'cap', None) == 'max_connections']


async def test_zero_disables_the_cap():
    async with served(_app, max_connections=0) as s:
        await asyncio.gather(*(_held(s) for _ in range(8)))
