"""A request target is origin-form, or ``*`` for OPTIONS.

HTTP/2 ``:path`` admits nothing else (RFC 9113 §8.3.1): the stream is reset
with PROTOCOL_ERROR.  HTTP/1.1 also admits absolute-form (RFC 9112 §3.2.2) and
answers any other form 400.
"""
import asyncio

import pytest
from hpack import Encoder

from blackbull import BlackBull, Response
from tests.harness import read_h2_frame, served

pytestmark = pytest.mark.asyncio

HEADERS, RST_STREAM = 1, 3
PROTOCOL_ERROR = 1


def _frame(kind: int, flags: int, payload: bytes = b'', stream_id: int = 1) -> bytes:
    return (len(payload).to_bytes(3, 'big') + bytes([kind, flags])
            + stream_id.to_bytes(4, 'big') + payload)


async def _ending(method: bytes, path: bytes) -> tuple[int, bytes]:
    app = BlackBull()

    @app.route(path='/x')
    async def x(conn):
        return Response(b'X')

    async with served(app) as s:
        reader, writer = await asyncio.open_connection('127.0.0.1', s.server.port)
        block = Encoder().encode([(b':method', method), (b':path', path),
                                  (b':scheme', b'http'), (b':authority', b'a')])
        writer.write(b'PRI * HTTP/2.0\r\n\r\nSM\r\n\r\n' + _frame(4, 0, stream_id=0)
                     + _frame(HEADERS, 0x5, block))
        try:
            while True:
                kind, _flags, stream_id, payload = await read_h2_frame(reader)
                if stream_id == 1 and kind in (HEADERS, RST_STREAM):
                    return kind, payload
        finally:
            writer.close()


@pytest.mark.parametrize('method,path', [
    (b'GET', b'http://ignored/x'), (b'GET', b'https://ignored/x'),
    (b'GET', b'http://ignored'), (b'GET', b'x'), (b'GET', b'?q'), (b'GET', b'*'),
])
async def test_a_path_that_is_not_origin_form_is_malformed(method, path):
    kind, payload = await _ending(method, path)
    assert (kind, int.from_bytes(payload, 'big')) == (RST_STREAM, PROTOCOL_ERROR)


@pytest.mark.parametrize('method,path', [
    (b'GET', b'/x'), (b'GET', b'/x?q=1'), (b'GET', b'//x'), (b'OPTIONS', b'*'),
])
async def test_origin_form_and_options_asterisk_are_served(method, path):
    kind, _payload = await _ending(method, path)
    assert kind == HEADERS


@pytest.mark.parametrize('target,status', [
    (b'x', 400), (b'?q', 400), (b'http://ignored/x', 200), (b'/x', 200),
])
async def test_http1_admits_absolute_form_and_refuses_a_bare_path(target, status):
    app = BlackBull()

    @app.route(path='/x')
    async def x(conn):
        return Response(b'X')

    async with served(app) as s:
        wire = await s.wire()
        await wire.send(b'GET ' + target + b' HTTP/1.1\r\nHost: a\r\n\r\n')
        assert (await wire.response()).status == status
