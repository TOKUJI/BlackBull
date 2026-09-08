"""HTTP/1.1 response framing is derived from one server-owned policy."""

from http import HTTPStatus

import pytest

from blackbull.native import NativeResponse
from blackbull.response import Response, StreamingResponse
from blackbull.server.sender import AbstractWriter, HTTP1Sender


class MemoryWriter(AbstractWriter):
    def __init__(self) -> None:
        self.data = bytearray()

    async def write(self, data: bytes) -> None:
        self.data.extend(data)


def _split(wire: bytes) -> tuple[bytes, bytes]:
    return wire.split(b'\r\n\r\n', 1)


def _values(head: bytes, name: bytes) -> list[bytes]:
    prefix = name.lower() + b':'
    return [
        line.split(b':', 1)[1].strip()
        for line in head.split(b'\r\n')[1:]
        if line.lower().startswith(prefix)
    ]


async def _send_fixed(lane: str, headers, body: bytes = b'ok',
                      status: int = 200, *, head: bool = False,
                      writer: MemoryWriter | None = None):
    writer = writer or MemoryWriter()
    sender = HTTP1Sender(writer)
    sender._head_mode = head
    if lane == 'dict':
        await sender({'type': 'http.response.start', 'status': status,
                      'headers': headers})
        await sender({'type': 'http.response.body', 'body': body,
                      'more_body': False})
    elif lane == 'native':
        await sender(NativeResponse(status=status, header=list(headers),
                                    body=body))
    elif lane == 'bytes':
        await sender(body, HTTPStatus(status), headers)
    elif lane == 'response':
        response = Response(body, status=HTTPStatus(status), headers=headers)
        await sender(response.to_native())
    else:
        raise AssertionError(f'unknown lane {lane}')
    return sender, bytes(writer.data)


@pytest.mark.parametrize('lane', ['dict', 'native', 'bytes', 'response'])
@pytest.mark.asyncio
async def test_fixed_body_ignores_application_transfer_encoding(lane):
    _sender, wire = await _send_fixed(
        lane, [(b'transfer-encoding', b'gzip, chunked')])
    head, body = _split(wire)
    assert _values(head, b'transfer-encoding') == []
    assert _values(head, b'content-length') == [b'2']
    assert body == b'ok'


@pytest.mark.parametrize('lane', ['dict', 'native', 'bytes', 'response'])
@pytest.mark.asyncio
async def test_fixed_body_rejects_a_mismatched_content_length_before_wire(
        lane):
    writer = MemoryWriter()
    with pytest.raises(ValueError, match='Content-Length'):
        await _send_fixed(lane, [(b'content-length', b'5')], writer=writer)
    assert writer.data == b''


@pytest.mark.asyncio
async def test_equal_content_lengths_are_normalized_to_one_field():
    _sender, wire = await _send_fixed('dict', [
        (b'content-length', b' 02 '),
        (b'Content-Length', b'2, 02'),
    ])
    head, body = _split(wire)
    assert _values(head, b'content-length') == [b'2']
    assert body == b'ok'


@pytest.mark.parametrize('values', [
    [b'2', b'3'],
    [b'2, 3'],
    [b'two'],
    [b'-1'],
    [b''],
])
@pytest.mark.asyncio
async def test_ambiguous_content_length_is_rejected_before_wire(values):
    headers = [(b'content-length', value) for value in values]
    writer = MemoryWriter()
    sender = HTTP1Sender(writer)
    await sender({'type': 'http.response.start', 'status': 200,
                  'headers': headers})
    with pytest.raises(ValueError, match='Content-Length'):
        await sender({'type': 'http.response.body', 'body': b'ok'})
    assert writer.data == b''
    assert sender._started is False


@pytest.mark.parametrize('lane', ['dict', 'native'])
@pytest.mark.asyncio
async def test_known_length_stream_keeps_one_content_length_and_raw_body(lane):
    writer = MemoryWriter()
    sender = HTTP1Sender(writer)
    headers = [(b'content-length', b'2'),
               (b'transfer-encoding', b'gzip')]
    if lane == 'dict':
        await sender({'type': 'http.response.start', 'status': 200,
                      'headers': headers})
        await sender({'type': 'http.response.body', 'body': b'a',
                      'more_body': True})
        await sender({'type': 'http.response.body', 'body': b'b',
                      'more_body': False})
    else:
        await sender(NativeResponse(status=200, header=headers))
        await sender(NativeResponse(body=b'a', more_body=True))
        await sender(NativeResponse(body=b'b'))

    head, body = _split(bytes(writer.data))
    assert _values(head, b'content-length') == [b'2']
    assert _values(head, b'transfer-encoding') == []
    assert body == b'ab'
    assert sender._completed is True


@pytest.mark.asyncio
async def test_streaming_response_preserves_a_correct_known_length():
    async def chunks():
        yield b'a'
        yield b'b'

    writer = MemoryWriter()
    sender = HTTP1Sender(writer)
    response = StreamingResponse(
        chunks(), headers=[
            (b'content-length', b'2'),
            (b'transfer-encoding', b'gzip'),
        ])
    await response(None, None, sender)

    head, body = _split(bytes(writer.data))
    assert _values(head, b'content-length') == [b'2']
    assert _values(head, b'transfer-encoding') == []
    assert body == b'ab'


@pytest.mark.asyncio
async def test_unknown_length_stream_replaces_application_transfer_encoding():
    writer = MemoryWriter()
    sender = HTTP1Sender(writer)
    await sender({'type': 'http.response.start', 'status': 200,
                  'headers': [(b'transfer-encoding', b'gzip')]})
    await sender({'type': 'http.response.body', 'body': b'a',
                  'more_body': True})
    await sender({'type': 'http.response.body', 'body': b'b',
                  'more_body': False})

    head, body = _split(bytes(writer.data))
    assert _values(head, b'content-length') == []
    assert _values(head, b'transfer-encoding') == [b'chunked']
    assert body == b'1\r\na\r\n1\r\nb\r\n0\r\n\r\n'


@pytest.mark.parametrize('first,last', [(b'a', b'bc'), (b'a', b'')])
@pytest.mark.asyncio
async def test_known_length_stream_rejects_overflow_or_short_terminal_chunk(
        first, last):
    writer = MemoryWriter()
    sender = HTTP1Sender(writer)
    await sender({'type': 'http.response.start', 'status': 200,
                  'headers': [(b'content-length', b'2')]})
    await sender({'type': 'http.response.body', 'body': first,
                  'more_body': True})
    with pytest.raises(ValueError, match='Content-Length'):
        await sender({'type': 'http.response.body', 'body': last,
                      'more_body': False})
    assert sender._completed is False
    failed_wire = bytes(writer.data)
    await sender(b'error', HTTPStatus.INTERNAL_SERVER_ERROR)
    assert bytes(writer.data) == failed_wire
    assert sender._poisoned is True


@pytest.mark.asyncio
async def test_known_length_stream_may_finish_with_an_empty_terminal_event():
    writer = MemoryWriter()
    sender = HTTP1Sender(writer)
    await sender({'type': 'http.response.start', 'status': 200,
                  'headers': [(b'content-length', b'2')]})
    await sender({'type': 'http.response.body', 'body': b'ab',
                  'more_body': True})
    await sender({'type': 'http.response.body', 'body': b'',
                  'more_body': False})

    _head, body = _split(bytes(writer.data))
    assert body == b'ab'
    assert sender._completed is True


@pytest.mark.asyncio
async def test_known_length_stream_rejects_first_chunk_overflow_before_wire():
    writer = MemoryWriter()
    sender = HTTP1Sender(writer)
    await sender({'type': 'http.response.start', 'status': 200,
                  'headers': [(b'content-length', b'1')]})
    with pytest.raises(ValueError, match='Content-Length'):
        await sender({'type': 'http.response.body', 'body': b'ab',
                      'more_body': True})
    assert writer.data == b''
    assert sender._started is False


@pytest.mark.asyncio
async def test_no_content_status_removes_framing_and_discards_body():
    sender, wire = await _send_fixed(
        'dict',
        [(b'content-length', b'2'), (b'transfer-encoding', b'chunked')],
        status=204,
    )
    head, body = _split(wire)
    assert _values(head, b'content-length') == []
    assert _values(head, b'transfer-encoding') == []
    assert body == b''
    assert sender._completed is True


@pytest.mark.asyncio
async def test_reset_content_uses_an_explicit_zero_length_boundary():
    sender, wire = await _send_fixed(
        'dict',
        [(b'content-length', b'2'), (b'transfer-encoding', b'chunked')],
        status=205,
    )
    head, body = _split(wire)
    assert _values(head, b'content-length') == [b'0']
    assert _values(head, b'transfer-encoding') == []
    assert body == b''
    assert sender._completed is True


@pytest.mark.asyncio
async def test_not_modified_preserves_metadata_length_but_discards_body():
    sender, wire = await _send_fixed(
        'dict',
        [(b'content-length', b'99'), (b'transfer-encoding', b'chunked')],
        status=304,
    )
    head, body = _split(wire)
    assert _values(head, b'content-length') == [b'99']
    assert _values(head, b'transfer-encoding') == []
    assert body == b''
    assert sender._completed is True


@pytest.mark.asyncio
async def test_not_modified_without_metadata_length_does_not_invent_one():
    _sender, wire = await _send_fixed('dict', [], status=304)
    head, body = _split(wire)
    assert _values(head, b'content-length') == []
    assert body == b''


@pytest.mark.asyncio
async def test_informational_body_is_discarded_before_the_final_response():
    writer = MemoryWriter()
    sender = HTTP1Sender(writer)
    await sender({'type': 'http.response.start', 'status': 103,
                  'headers': [(b'content-length', b'2'),
                              (b'transfer-encoding', b'chunked')]})
    await sender({'type': 'http.response.body', 'body': b'no'})
    await sender({'type': 'http.response.start', 'status': 200, 'headers': []})
    await sender({'type': 'http.response.body', 'body': b'yes'})

    first, second = bytes(writer.data).split(b'HTTP/1.1 200 OK', 1)
    assert _values(first.split(b'\r\n\r\n', 1)[0], b'content-length') == []
    assert _values(first.split(b'\r\n\r\n', 1)[0], b'transfer-encoding') == []
    assert first.endswith(b'\r\n\r\n')
    assert second.endswith(b'\r\n\r\nyes')


@pytest.mark.asyncio
async def test_head_keeps_computed_length_but_discards_body_and_app_te():
    sender, wire = await _send_fixed(
        'dict', [(b'transfer-encoding', b'gzip')], head=True)
    head, body = _split(wire)
    assert _values(head, b'content-length') == [b'2']
    assert _values(head, b'transfer-encoding') == []
    assert body == b''
    assert sender._completed is True


@pytest.mark.asyncio
async def test_bodyless_status_does_not_open_a_trailer_section():
    writer = MemoryWriter()
    sender = HTTP1Sender(writer)
    await sender({'type': 'http.response.start', 'status': 204,
                  'headers': [(b'content-length', b'2')], 'trailers': True})
    await sender({'type': 'http.response.body', 'body': b'ok'})
    completed = bytes(writer.data)
    await sender({'type': 'http.response.trailers',
                  'headers': [(b'x-end', b'yes')]})

    head, body = _split(bytes(writer.data))
    assert bytes(writer.data) == completed
    assert _values(head, b'content-length') == []
    assert _values(head, b'transfer-encoding') == []
    assert body == b''


@pytest.mark.asyncio
async def test_declared_trailers_remain_chunked_and_complete_on_trailers():
    writer = MemoryWriter()
    sender = HTTP1Sender(writer)
    await sender({'type': 'http.response.start', 'status': 200,
                  'headers': [(b'content-length', b'not-a-length'),
                              (b'transfer-encoding', b'gzip')],
                  'trailers': True})
    await sender({'type': 'http.response.body', 'body': b'ok'})
    await sender({'type': 'http.response.trailers',
                  'headers': [(b'x-end', b'yes')]})

    head, body = _split(bytes(writer.data))
    assert _values(head, b'content-length') == []
    assert _values(head, b'transfer-encoding') == [b'chunked']
    assert body == b'2\r\nok\r\n0\r\nx-end: yes\r\n\r\n'
    assert sender._completed is True
