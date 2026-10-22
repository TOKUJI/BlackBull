"""RFC 9110 §6.5.1 fields a trailer may not carry reset an HTTP/2 stream.

HTTP/1.1 trailer sections are rows of tests/conformance/http1/test_framing_both_roles.py.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from hpack import Encoder
import pytest

from blackbull.protocol.frame_types import ErrorCodes, FrameTypes, HeaderFrameFlags
from blackbull.server.http2_actor import HTTP2Actor
from blackbull.server.sender import AsyncioWriter


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
