"""StaticFiles applies ``Range`` only when ``If-Range`` still matches (RFC 9110 §13.1.5).

A stale validator gets the whole current representation, never a slice of it.
"""
from email.utils import formatdate

import pytest

from blackbull import BlackBull
from tests.harness import served

pytestmark = pytest.mark.asyncio

BODY = b'hello world'


async def _get(tmp_path, headers: bytes):
    (tmp_path / 'f.txt').write_bytes(BODY)
    app = BlackBull()
    app.static('/s', str(tmp_path))
    async with served(app) as s:
        w = await s.wire()
        await w.send(b'GET /s/f.txt HTTP/1.1\r\nHost: x\r\n\r\n')
        first = await w.response()
        etag = first.header(b'etag')
        mtime = (tmp_path / 'f.txt').stat().st_mtime
        w = await s.wire()
        await w.send(b'GET /s/f.txt HTTP/1.1\r\nHost: x\r\nRange: bytes=0-2\r\n'
                     + headers.replace(b'ETAG', etag)
                     .replace(b'BEFORE', formatdate(mtime - 3600, usegmt=True).encode())
                     .replace(b'AFTER', formatdate(mtime + 3600, usegmt=True).encode())
                     + b'\r\n')
        return await w.response()


@pytest.mark.parametrize('if_range', [b'If-Range: ETAG\r\n', b'If-Range: AFTER\r\n', b''])
async def test_a_matching_or_absent_validator_gets_the_range(tmp_path, if_range):
    reply = await _get(tmp_path, if_range)
    assert (reply.status, reply.body) == (206, b'hel')


@pytest.mark.parametrize('if_range', [
    b'If-Range: "stale"\r\n', b'If-Range: W/ETAG\r\n', b'If-Range: BEFORE\r\n',
    b'If-Range: not a date\r\n'])
async def test_a_stale_weak_or_unreadable_validator_gets_the_whole_file(tmp_path, if_range):
    reply = await _get(tmp_path, if_range)
    assert (reply.status, reply.body) == (200, BODY)
    assert reply.header(b'content-range') is None
