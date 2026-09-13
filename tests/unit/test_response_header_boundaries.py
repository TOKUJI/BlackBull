"""Outbound HTTP field boundaries shared by response helpers and senders."""
import asyncio

import pytest

from blackbull import JSONResponse, RedirectResponse, Response, StreamingResponse
from blackbull.native import NativeResponse
from blackbull.protocol.frame import FrameFactory
from blackbull.response import cookie_header
from blackbull.server.sender import AbstractWriter, HTTP1Sender, HTTP2Sender


class _Writer(AbstractWriter):
    def __init__(self):
        self.data = bytearray()

    async def write(self, data: bytes) -> None:
        self.data.extend(data)


@pytest.mark.parametrize('name', [
    b'',
    b'x bad',
    b'x:bad',
    b'x\r\nx-added',
    b'x\x80',
])
def test_response_rejects_non_token_field_names(name):
    with pytest.raises(ValueError, match='header name'):
        Response(b'', headers=[(name, b'value')])


@pytest.mark.parametrize('value', [
    b'before\x00after',
    b'before\r\nx-added: value',
    b'before\nafter',
    b'before\x01after',
    b'before\x1fafter',
    b'before\x7fafter',
])
def test_response_rejects_prohibited_field_value_octets(value):
    with pytest.raises(ValueError, match='header value'):
        Response(b'', headers=[(b'x-origin', value)])


def test_all_response_helpers_reject_boundary_breaking_values():
    async def chunks():
        yield b'ok'

    with pytest.raises(ValueError, match='header value'):
        JSONResponse({}, headers=[(b'x-origin', b'a\r\nx-added: b')])
    with pytest.raises(ValueError, match='header value'):
        RedirectResponse('a\r\nx-added: b')
    with pytest.raises(ValueError, match='header value'):
        StreamingResponse(
            chunks(), headers=[(b'x-origin', b'a\r\nx-added: b')])
    with pytest.raises(ValueError, match='header value'):
        StreamingResponse(chunks(), media_type='text/plain\r\nx-added: b')
    with pytest.raises(ValueError, match='header value'):
        cookie_header('session', 'a\r\nx-added: b')


def test_response_preserves_legal_field_value_controls_and_duplicates():
    headers = [
        (b'x-empty', b''),
        (b'x-tab', b'left\tright'),
        (b'x-obs', b'\x80'),
        (b'set-cookie', b'a=1'),
        (b'set-cookie', b'b=2'),
    ]
    response = Response(b'', headers=headers)
    assert response.headers[1:] == headers


@pytest.mark.asyncio
@pytest.mark.parametrize('event', [
    NativeResponse(status=200, header=[
        (b'x-origin', b'a\r\nx-added: b')]),
    {'type': 'http.response.start', 'status': 200,
     'headers': [(b'x-origin', b'a\r\nx-added: b')]},
])
async def test_h1_rejects_invalid_native_or_asgi_head_before_buffering(event):
    writer = _Writer()
    sender = HTTP1Sender(writer)

    with pytest.raises(ValueError, match='header value'):
        await sender(event)

    assert writer.data == b''


@pytest.mark.asyncio
async def test_h1_rejects_invalid_native_field_name_before_buffering():
    writer = _Writer()
    sender = HTTP1Sender(writer)

    with pytest.raises(ValueError, match='header name'):
        await sender(NativeResponse(
            status=200, header=[(b'x-origin\r\nx-added', b'value')]))

    assert writer.data == b''


@pytest.mark.asyncio
async def test_h1_final_boundary_rechecks_mutated_response_headers():
    native = Response(b'').to_native()
    assert native.header is not None
    native.header.append(b'x-origin', b'a\r\nx-added: b')
    writer = _Writer()

    with pytest.raises(ValueError, match='header value'):
        await HTTP1Sender(writer)(native)

    assert writer.data == b''


@pytest.mark.asyncio
async def test_h1_validates_entire_trailer_event_before_writing_any_of_it():
    writer = _Writer()
    sender = HTTP1Sender(writer)
    await sender({'type': 'http.response.start', 'status': 200,
                  'headers': [], 'trailers': True})
    await sender({'type': 'http.response.body', 'body': b'ok',
                  'more_body': False})
    before = bytes(writer.data)

    with pytest.raises(ValueError, match='header value'):
        await sender({
            'type': 'http.response.trailers',
            'headers': [
                (b'x-valid', b'yes'),
                (b'x-origin', b'a\r\nx-added: b'),
            ],
        })

    assert bytes(writer.data) == before


@pytest.mark.asyncio
@pytest.mark.parametrize('event', [
    NativeResponse(status=200, header=[
        (b'x-origin', b'a\r\nx-added: b')]),
    {'type': 'http.response.start', 'status': 200,
     'headers': [(b'x-origin', b'a\r\nx-added: b')]},
])
async def test_h2_rejects_invalid_native_or_asgi_head_before_hpack(event):
    writer = _Writer()
    factory = FrameFactory()
    sender = HTTP2Sender(writer, factory, stream_id=1)

    with pytest.raises(ValueError, match='header value'):
        await sender(event)

    assert writer.data == b''
    assert len(factory.encoder.header_table.dynamic_entries) == 0


@pytest.mark.asyncio
async def test_h2_rejects_invalid_asgi_field_name_before_hpack():
    writer = _Writer()
    factory = FrameFactory()
    sender = HTTP2Sender(writer, factory, stream_id=1)

    with pytest.raises(ValueError, match='header name'):
        await sender({
            'type': 'http.response.start', 'status': 200,
            'headers': [(b'x-origin\r\nx-added', b'value')],
        })

    assert writer.data == b''
    assert len(factory.encoder.header_table.dynamic_entries) == 0


@pytest.mark.asyncio
async def test_h2_validates_entire_trailer_event_before_hpack_or_write():
    writer = _Writer()
    factory = FrameFactory()
    sender = HTTP2Sender(writer, factory, stream_id=1)
    await sender({'type': 'http.response.start', 'status': 200,
                  'headers': [], 'trailers': True})
    await sender({'type': 'http.response.body', 'body': b'ok',
                  'more_body': False})
    before = bytes(writer.data)
    dynamic_entries = tuple(factory.encoder.header_table.dynamic_entries)

    with pytest.raises(ValueError, match='header value'):
        await sender({
            'type': 'http.response.trailers',
            'headers': [
                (b'x-valid', b'yes'),
                (b'x-origin', b'a\r\nx-added: b'),
            ],
        })

    assert bytes(writer.data) == before
    assert tuple(factory.encoder.header_table.dynamic_entries) == dynamic_entries


@pytest.mark.asyncio
async def test_h1_legal_controls_reach_one_field_each():
    writer = _Writer()
    sender = HTTP1Sender(writer)
    headers = [
        (b'x-empty', b''),
        (b'x-tab', b'left\tright'),
        (b'x-obs', b'\x80'),
        (b'set-cookie', b'a=1'),
        (b'set-cookie', b'b=2'),
    ]
    await sender(NativeResponse(status=200, header=headers, body=b''))
    wire = bytes(writer.data)

    assert wire.count(b'x-empty: \r\n') == 1
    assert wire.count(b'x-tab: left\tright\r\n') == 1
    assert wire.count(b'x-obs: \x80\r\n') == 1
    assert wire.count(b'set-cookie:') == 2


@pytest.mark.parametrize('native', [
    NativeResponse(status=200, header=[
        (b'x-origin', b'a\r\nx-added: b')], body=b''),
    NativeResponse(status=200, header=[], body=b'', trailers=[
        (b'x-origin', b'a\r\nx-added: b')]),
])
def test_external_asgi_conversion_validates_all_sections_before_return(native):
    with pytest.raises(ValueError, match='header value'):
        native.to_asgi()
