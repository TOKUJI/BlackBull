"""Outbound HTTP fields are checked once, when a NativeResponse is built.

A response helper builds its NativeResponse when it is sent (``to_native``);
a NativeResponse checks its fields when constructed, assigned or appended
to; an ASGI ``http.response.*`` dict is checked when converted.  Nothing that
breaks a field reaches the wire or the HPACK table.
"""
import asyncio
from http import HTTPStatus

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
        Response(b'', headers=[(name, b'value')]).to_native()


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
        Response(b'', headers=[(b'x-origin', value)]).to_native()


@pytest.mark.asyncio
async def test_all_response_helpers_reject_boundary_breaking_values():
    async def chunks():
        yield b'ok'

    async def send(event):
        pass

    with pytest.raises(ValueError, match='header value'):
        JSONResponse({}, headers=[(b'x-origin', b'a\r\nx-added: b')]).to_native()
    with pytest.raises(ValueError, match='header value'):
        RedirectResponse('a\r\nx-added: b').to_native()
    with pytest.raises(ValueError, match='header value'):
        await StreamingResponse(
            chunks(), headers=[(b'x-origin', b'a\r\nx-added: b')])(None, None, send)
    with pytest.raises(ValueError, match='header value'):
        await StreamingResponse(
            chunks(), media_type='text/plain\r\nx-added: b')(None, None, send)
    with pytest.raises(ValueError, match='header value'):
        NativeResponse(header=[cookie_header('session', 'a\r\nx-added: b')])


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


@pytest.mark.parametrize('field, message', [
    ((b'x-origin', b'a\r\nx-added: b'), 'header value'),
    ((b'x-origin\r\nx-added', b'value'), 'header name'),
    ((b'', b'value'), 'header name'),
])
def test_a_native_response_refuses_a_breaking_field_when_built(field, message):
    with pytest.raises(ValueError, match=message):
        NativeResponse(status=200, header=[field])


@pytest.mark.parametrize('field, message', [
    ((b'', b'value'), 'header name'),
    ((b'x-extra', b'value', b'third'), 'unpack'),
], ids=['empty-name', 'three-items'])
def test_assigning_a_malformed_field_is_refused(field, message):
    native = NativeResponse(status=200)
    with pytest.raises(ValueError, match=message):
        native.header = [(b'x-ok', b'value'), field]


def test_appending_a_breaking_field_is_refused():
    native = Response(b'').to_native()
    with pytest.raises(ValueError, match='header value'):
        native.header.append(b'x-origin', b'a\r\nx-added: b')


@pytest.mark.asyncio
async def test_h1_rejects_an_invalid_asgi_head_before_buffering():
    writer = _Writer()

    with pytest.raises(ValueError, match='header value'):
        await HTTP1Sender(writer)({'type': 'http.response.start', 'status': 200,
                                   'headers': [(b'x-origin', b'a\r\nx-added: b')]})

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
async def test_h2_rejects_an_invalid_asgi_head_before_hpack():
    writer = _Writer()
    factory = FrameFactory()
    sender = HTTP2Sender(writer, factory, stream_id=1)

    with pytest.raises(ValueError, match='header value'):
        await sender({'type': 'http.response.start', 'status': 200,
                      'headers': [(b'x-origin', b'a\r\nx-added: b')]})

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


@pytest.mark.parametrize('sections', [
    {'header': [(b'x-origin', b'a\r\nx-added: b')]},
    {'header': [], 'trailers': [(b'x-origin', b'a\r\nx-added: b')]},
])
def test_every_section_is_checked_when_built(sections):
    with pytest.raises(ValueError, match='header value'):
        NativeResponse(status=200, body=b'', **sections)


@pytest.mark.asyncio
@pytest.mark.parametrize('protocol', ['h1', 'h2'])
@pytest.mark.parametrize('native', [False, True])
async def test_buffered_start_owns_its_header_snapshot(protocol, native):
    writer = _Writer()
    factory = FrameFactory()
    sender = (HTTP1Sender(writer) if protocol == 'h1'
              else HTTP2Sender(writer, factory, 1))
    headers = [(b'x-original', b'1')]
    start = (NativeResponse(header=headers) if native else
             {'type': 'http.response.start', 'status': 200, 'headers': headers})
    await sender(start)
    headers.append((b'x-late', b'2'))
    await sender(NativeResponse(body=b'ok'))
    if protocol == 'h1':
        assert b'x-original: 1\r\n' in writer.data
        assert b'x-late:' not in writer.data
    else:
        length = int.from_bytes(writer.data[:3], 'big')
        pairs = factory.decoder.decode(bytes(writer.data[9:9 + length]), raw=True)
        assert (b'x-original', b'1') in pairs
        assert not any(k == b'x-late' for k, _ in pairs)
    assert headers == [(b'x-original', b'1'), (b'x-late', b'2')]


@pytest.mark.parametrize('attempt', [1, 2])
def test_an_invalid_content_type_is_refused_every_time(attempt):
    with pytest.raises(ValueError):
        Response(b'', content_type='text/plain\r\nx-injected: 1').to_native()


def test_a_content_type_becomes_its_pair():
    assert Response(b'', content_type='text/plain').headers == [
        (b'content-type', b'text/plain')]


_MIXED = [(b'Content-Type', b'text/plain'), (b'X-Trace', b'1')]


def _h2_blocks(writer, factory) -> list:
    """The field list of every HEADERS frame on the wire, in order."""
    data, blocks = bytes(writer.data), []
    while data:
        length = int.from_bytes(data[:3], 'big')
        if data[3] == 0x1:
            blocks.append(factory.decoder.decode(data[9:9 + length], raw=True))
        data = data[9 + length:]
    return blocks


async def _send_mixed_case_head(sender, form):
    if form == 'native':
        await sender(NativeResponse(status=200, header=list(_MIXED), body=b'ok'))
    elif form == 'asgi':
        await sender({'type': 'http.response.start', 'status': 200,
                      'headers': list(_MIXED)})
        await sender({'type': 'http.response.body', 'body': b'ok'})
    else:
        await sender(b'ok', HTTPStatus.OK, list(_MIXED))


@pytest.mark.asyncio
@pytest.mark.parametrize('form', ['native', 'asgi', 'bytes'])
async def test_h1_sends_response_field_names_lowercase(form):
    writer = _Writer()
    await _send_mixed_case_head(HTTP1Sender(writer), form)
    head = bytes(writer.data).split(b'\r\n\r\n', 1)[0]
    names = [line.split(b':', 1)[0] for line in head.split(b'\r\n')[1:]]
    assert {b'content-type', b'x-trace', b'date'} <= set(names)
    assert names == [name.lower() for name in names]


@pytest.mark.asyncio
@pytest.mark.parametrize('form', ['native', 'asgi', 'bytes'])
async def test_h2_sends_response_field_names_lowercase(form):
    """RFC 9113 §8.2.2 (BLA-524)."""
    writer, factory = _Writer(), FrameFactory()
    await _send_mixed_case_head(HTTP2Sender(writer, factory, 1), form)
    [fields] = _h2_blocks(writer, factory)
    assert (b'content-type', b'text/plain') in fields
    assert (b'x-trace', b'1') in fields
    assert all(name == name.lower() for name, _ in fields)


@pytest.mark.asyncio
@pytest.mark.parametrize('protocol', ['h1', 'h2'])
async def test_trailer_field_names_leave_lowercase(protocol):
    writer, factory = _Writer(), FrameFactory()
    sender = (HTTP1Sender(writer) if protocol == 'h1'
              else HTTP2Sender(writer, factory, 1))
    await sender({'type': 'http.response.start', 'status': 200,
                  'headers': [], 'trailers': True})
    await sender({'type': 'http.response.body', 'body': b'ok'})
    await sender({'type': 'http.response.trailers',
                  'headers': [(b'Grpc-Status', b'0')]})
    if protocol == 'h1':
        assert bytes(writer.data).endswith(b'0\r\ngrpc-status: 0\r\n\r\n')
    else:
        assert _h2_blocks(writer, factory)[-1] == [(b'grpc-status', b'0')]


def test_external_asgi_events_carry_lowercase_field_names():
    events = NativeResponse(status=200, header=list(_MIXED), body=b'',
                            trailers=[(b'X-Sum', b'1')]).to_asgi()
    assert list(events[0]['headers']) == [
        (b'content-type', b'text/plain'), (b'x-trace', b'1')]
    assert list(events[-1]['headers']) == [(b'x-sum', b'1')]
