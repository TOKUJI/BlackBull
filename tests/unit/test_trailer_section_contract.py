"""A trailer section obeys the header section's field-line grammar.

RFC 9112 §7.1.2 gives a trailer section the same field lines as a header
section, and RFC 9110 §6.5.1 lists fields a trailer may not carry.  A line
refused in a header section is refused in a trailer section, and a prohibited
trailer field is refused on both transports: HTTP/1.1 with 400, HTTP/2 with
RST_STREAM PROTOCOL_ERROR.
"""
from __future__ import annotations

from http import HTTPStatus
from unittest.mock import AsyncMock, MagicMock

from hpack import Encoder
import pytest

from blackbull.connection import Connection
from blackbull.headers import Headers
from blackbull.protocol.frame_types import ErrorCodes, FrameTypes, HeaderFrameFlags
from blackbull.router import HTTPException
from blackbull.server.http1_actor import BadRequestError, HTTP1Actor
from blackbull.server.http2_actor import HTTP2Actor
from blackbull.server.recipient import AbstractReader, HTTP1Recipient
from blackbull.server.sender import AsyncioWriter

#: Field lines the header section refuses.
_BAD_LINES = [
    pytest.param(b'Foo : bar', id='space-before-colon'),
    pytest.param(b' folded', id='obs-fold'),
    pytest.param(b'X\x01: y', id='name-control'),
    pytest.param(b'X-A: a\x00b', id='value-nul'),
    pytest.param(b'X-A: \x0bv', id='value-vt'),
    pytest.param(b'NoColon', id='no-colon'),
]


class _Wire(AbstractReader):
    def __init__(self, data: bytes) -> None:
        self._buf = bytearray(data)

    async def read(self, n: int) -> bytes:
        chunk = bytes(self._buf[:n])
        del self._buf[:n]
        return chunk


def _header_refused(line: bytes) -> bool:
    try:
        object.__new__(HTTP1Actor)._parse(
            b'POST / HTTP/1.1\r\nHost: a\r\n' + line + b'\r\n\r\n')
    except BadRequestError:
        return True
    return False


async def _read_chunked(wire: bytes) -> bytes:
    conn = Connection(method='POST', path='/', raw_path=b'/', type='http',
                      headers=Headers([(b'transfer-encoding', b'chunked')]))
    recipient = HTTP1Recipient(_Wire(wire), conn, chunk_size=64 * 1024)
    body = bytearray()
    while True:
        event = await recipient()
        body += event.get('body', b'')
        if not event.get('more_body', False):
            return bytes(body)


@pytest.mark.asyncio
class TestHttp1:
    @pytest.mark.parametrize('line', _BAD_LINES)
    async def test_a_line_refused_in_the_header_section_is_refused_as_a_trailer(
            self, line):
        assert _header_refused(line)
        with pytest.raises(HTTPException) as refused:
            await _read_chunked(b'1\r\nZ\r\n0\r\n' + line + b'\r\n\r\n')
        assert refused.value.status == HTTPStatus.BAD_REQUEST

    async def test_a_valid_trailer_is_read_and_discarded(self):
        body = await _read_chunked(b'1\r\nZ\r\n0\r\nX-Checksum:  abc \r\n\r\n')

        assert body == b'Z'


def _frame(type_byte, flags, stream_id, payload=b'') -> bytes:
    return (len(payload).to_bytes(3, 'big') + type_byte + bytes([flags])
            + stream_id.to_bytes(4, 'big') + payload)


async def _h2_refusals(trailer_fields: list[tuple[bytes, bytes]]) -> list:
    """RST_STREAM error codes sent for a POST whose trailers are *trailer_fields*."""
    async def app(conn, receive, send):
        while (await receive()).get('more_body', False):
            pass
        await send({'type': 'http.response.start', 'status': 200, 'headers': []})
        await send({'type': 'http.response.body', 'body': b'ok'})

    writer = MagicMock()
    writer.drain = AsyncMock()
    actor = HTTP2Actor(None, AsyncioWriter(writer), app, aggregator=None)
    actor.send_frame = AsyncMock()
    enc = Encoder()
    head = enc.encode([(b':method', b'POST'), (b':path', b'/x'),
                       (b':scheme', b'https'), (b':authority', b'a.example')])
    actor.receive = AsyncMock(side_effect=[
        _frame(FrameTypes.SETTINGS, 0, 0),
        _frame(FrameTypes.HEADERS, HeaderFrameFlags.END_HEADERS, 1, head),
        _frame(FrameTypes.DATA, 0, 1, b'hello'),
        _frame(FrameTypes.HEADERS,
               HeaderFrameFlags.END_HEADERS | HeaderFrameFlags.END_STREAM, 1,
               enc.encode(trailer_fields)),
        None])
    await actor.run()
    return [c.args[0].error_code for c in actor.send_frame.call_args_list
            if c.args[0].FrameType() == FrameTypes.RST_STREAM]


@pytest.mark.asyncio
class TestHttp2:
    @pytest.mark.parametrize('name', [b'content-length', b'host',
                                      b'authorization', b'content-type'])
    async def test_a_prohibited_trailer_field_resets_the_stream(self, name):
        assert await _h2_refusals([(name, b'1')]) == [ErrorCodes.PROTOCOL_ERROR]

    async def test_a_permitted_trailer_field_is_accepted(self):
        assert await _h2_refusals([(b'x-checksum', b'abc')]) == []
