"""A declared ``content-length`` must equal the DATA received when the request ends.

RFC 9113 §8.1.1.  The request can end on its head, on a DATA frame, or on a
trailer section; each end that falls short of the declaration resets the
stream with PROTOCOL_ERROR before the handler sees an empty or short body.
"""
import asyncio

import pytest
from hpack import Encoder

from blackbull import BlackBull, Response
from tests.harness import read_h2_frame, served

pytestmark = pytest.mark.asyncio

DATA, HEADERS, RST_STREAM, SETTINGS = 0, 1, 3, 4
END_STREAM, END_HEADERS = 0x1, 0x4
PROTOCOL_ERROR = 1


def _frame(kind: int, flags: int, payload: bytes = b'', stream_id: int = 1) -> bytes:
    return (len(payload).to_bytes(3, 'big') + bytes([kind, flags])
            + stream_id.to_bytes(4, 'big') + payload)


def _app(seen: list):
    app = BlackBull()

    @app.route(path='/', methods=['POST'])
    async def echo(conn):
        body = await conn.body()
        seen.append(body)
        return Response(f'len={len(body)}'.encode())

    return app


async def _outcome(declared: bytes | None, *frames) -> tuple[str, int | None, list]:
    """Send one POST on stream 1; return the stream's ending and its value."""
    seen: list = []
    encoder = Encoder()
    fields = [(b':method', b'POST'), (b':path', b'/'), (b':scheme', b'http'),
              (b':authority', b'x')]
    if declared is not None:
        fields.append((b'content-length', declared))
    async with served(_app(seen)) as s:
        reader, writer = await asyncio.open_connection('127.0.0.1', s.server.port)
        out = b'PRI * HTTP/2.0\r\n\r\nSM\r\n\r\n' + _frame(SETTINGS, 0, stream_id=0)
        for kind, flags, payload in frames:
            if kind == HEADERS:
                payload = encoder.encode(fields if payload is None else payload)
            out += _frame(kind, flags, payload)
        writer.write(out)
        try:
            while True:
                kind, flags, stream_id, payload = await asyncio.wait_for(
                    read_h2_frame(reader), 5)
                if stream_id != 1:
                    continue
                if kind == RST_STREAM:
                    return 'rst', int.from_bytes(payload, 'big'), seen
                if kind == HEADERS:
                    return 'response', None, seen
        finally:
            writer.close()


HEAD = (HEADERS, END_HEADERS, None)
TRAILERS = (HEADERS, END_HEADERS | END_STREAM, [(b'x-t', b'1')])


@pytest.mark.parametrize('declared,frames', [
    (b'5', [(HEADERS, END_HEADERS | END_STREAM, None)]),
    (b'5', [HEAD, (DATA, 0, b'abc'), TRAILERS]),
    (b'5', [HEAD, TRAILERS]),
], ids=['closed-at-head', 'short-then-trailers', 'no-data-then-trailers'])
async def test_a_request_ending_short_of_its_declaration_is_reset(declared, frames):
    ending, code, seen = await _outcome(declared, *frames)
    assert (ending, code) == ('rst', PROTOCOL_ERROR)
    assert seen == []


@pytest.mark.parametrize('declared,frames', [
    (b'0', [(HEADERS, END_HEADERS | END_STREAM, None)]),
    (None, [(HEADERS, END_HEADERS | END_STREAM, None)]),
    (b'5', [HEAD, (DATA, END_STREAM, b'hello')]),
    (b'5', [HEAD, (DATA, 0, b'hello'), TRAILERS]),
], ids=['declared-zero', 'undeclared', 'complete', 'complete-then-trailers'])
async def test_a_request_meeting_its_declaration_is_answered(declared, frames):
    ending, _code, seen = await _outcome(declared, *frames)
    assert ending == 'response'
    assert seen == [b'hello' if declared == b'5' else b'']
