import asyncio
from http import HTTPStatus
from types import SimpleNamespace
from unittest.mock import AsyncMock

from hpack import Decoder
import pytest

from blackbull.client.response import SettingsResponder as ClientSettings
from blackbull.client.http2 import HTTP2Client
from blackbull.native import _native_from_asgi
from blackbull.protocol.frame import FrameFactory
from blackbull.protocol.frame_types import ErrorCodes, FrameTypes, PseudoHeaders, serialize_field_block
from blackbull.server.response import SettingsResponder as ServerSettings
from blackbull.server.sender import AbstractWriter, HTTP2Sender, build_response_headers, build_trailers


FIELDS = [(b'x-large', b'~' * 40000), (b'x-state', b'reused')]


def frames(wire):
    result = []
    while wire:
        length = int.from_bytes(wire[:3], 'big')
        assert len(wire) >= 9 + length
        result.append((wire[3], wire[4], int.from_bytes(wire[5:9], 'big'), wire[9:9 + length]))
        wire = wire[9 + length:]
    return result


class Writer(AbstractWriter):
    def __init__(self):
        self.writes = []

    async def write(self, data):
        self.writes.append(data)


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['response', 'trailers', 'request', 'push'])
@pytest.mark.parametrize('limit', [16384, 32768])
async def test_large_field_blocks_decode_in_encoder_order(kind, limit):
    factory = FrameFactory()
    writer = Writer()
    sender = HTTP2Sender(writer, factory, 1)
    sender.apply_settings(max_frame_size=limit)
    peer = Decoder(max_header_list_size=100000)
    for index in range(3):
        if index == 1:
            factory.apply_peer_header_table_size([(1, 0), (1, 128)])
            peer.max_allowed_table_size = 128
        if kind == 'response':
            wire = build_response_headers(factory.encoder, 1, 200, FIELDS,
                                          end_stream=True, max_frame_size=limit)
        elif kind == 'trailers':
            wire = build_trailers(factory.encoder, 1, FIELDS, max_frame_size=limit)
        else:
            if kind == 'push':
                frame = factory.push_promise(1, 2, {PseudoHeaders.METHOD: 'GET'}, FIELDS)
            else:
                frame = factory.create(FrameTypes.HEADERS, 5, 1)
                frame.pseudo_headers = {PseudoHeaders.METHOD: 'GET'}
                frame.headers = FIELDS
            await sender(frame)
            wire = writer.writes[-1]
        emitted = frames(wire)
        assert len(emitted) > 1
        assert all(len(payload) <= limit and sid == 1 for _, _, sid, payload in emitted)
        assert emitted[0][0] == (5 if kind == 'push' else 1)
        assert emitted[0][1] == (0 if kind == 'push' else 1)
        assert all(kind == 9 and flags == (4 if i == len(emitted) - 1 else 0)
                   for i, (kind, flags, _, _) in enumerate(emitted[1:], 1))
        block = b''.join(payload for _, _, _, payload in emitted)
        if kind == 'push':
            assert block[:4] == b'\x00\x00\x00\x02'
            block = block[4:]
        decoded = peer.decode(block, raw=True)
        assert all(field in decoded for field in FIELDS)


@pytest.mark.asyncio
@pytest.mark.parametrize('side', ['server', 'client'])
@pytest.mark.parametrize('sizes', [(32768,), (32768, 16384), (1, 32768), (2**24, 16384)])
async def test_frame_setting_is_connection_owned_and_validates_every_entry(side, sizes):
    factory = FrameFactory()
    writer = Writer()
    existing = HTTP2Sender(writer, factory, 1)
    control = HTTP2Sender(writer, factory, 0)
    error = AsyncMock()
    sent = AsyncMock()
    handler = SimpleNamespace(factory=factory, _factory=factory,
                              _senders={1: existing}, send_frame=sent,
                              _send_raw_frame=sent, _connection_error=error,
                              _fail_connection=error)
    data = b'\x00\x01\x00\x00\x00\x00' + b''.join(
        b'\x00\x05' + size.to_bytes(4, 'big') for size in sizes)
    responder = ServerSettings if side == 'server' else ClientSettings
    await responder(factory.create(FrameTypes.SETTINGS, 0, 0, data=data)).respond(handler)
    valid = all(16384 <= size < 2**24 for size in sizes)
    assert sent.await_count == int(valid)
    assert error.await_count == int(not valid)
    assert factory.encoder.header_table_size == (0 if valid else 4096)
    for sender in (existing, control, HTTP2Sender(writer, factory, 3)):
        assert sender.max_frame_size == (sizes[-1] if valid else 16384)


@pytest.mark.asyncio
async def test_fragmented_block_is_enqueued_before_drain_and_cancellation():
    entered = asyncio.Event()
    release = asyncio.Event()

    class ParkedWriter(Writer):
        async def write(self, data):
            await super().write(data)
            if not entered.is_set():
                entered.set()
                await release.wait()

    factory = FrameFactory()
    writer = ParkedWriter()
    first = HTTP2Sender(writer, factory, 1)
    second = HTTP2Sender(writer, factory, 3)
    task = asyncio.create_task(first.send_response_headers(HTTPStatus.OK, FIELDS))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        second.apply_settings(max_frame_size=32768)
        await second(factory.create(FrameTypes.PING, 1, 0, data=b'12345678'))
        await second(factory.rst_stream(5, ErrorCodes.CANCEL))
        await second._write_data(b'ok', end_stream=False)
        await second.send_response_headers(HTTPStatus.OK, [(b'x-state', b'reused')])
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert len(frames(writer.writes[0])) > 1
        peer = Decoder(max_header_list_size=100000)
        for wire in writer.writes:
            emitted = frames(wire)
            if emitted[0][0] != 1:
                continue
            assert emitted[-1][1] & 4
            peer.decode(b''.join(payload for _, _, _, payload in emitted))
    finally:
        release.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize('kind,prefix', [(b'\x01', b''), (b'\x05', b'\x00\x00\x00\x02')])
@pytest.mark.parametrize('size', [0, 16383, 16384, 16385, 32768, 32769])
def test_field_block_boundaries(kind, prefix, size):
    payload = prefix + b'x' * max(0, size - len(prefix))
    flags = 5 if kind == b'\x01' else 4
    emitted = frames(serialize_field_block(payload, kind, flags, 7, 16384))
    assert len(emitted) == max(1, (len(payload) + 16383) // 16384)
    assert b''.join(part for _, _, _, part in emitted) == payload
    assert emitted[0][:3] == (kind[0], flags if len(emitted) == 1 else flags & ~4, 7)
    for i, (type_, flags, sid, part) in enumerate(emitted):
        assert len(part) <= 16384 and sid == 7
        if i:
            assert type_ == 9
            assert flags == (4 if i == len(emitted) - 1 else 0)


@pytest.mark.asyncio
async def test_raw_client_escape_preserves_oversized_frame():
    client = HTTP2Client('localhost', 1)
    writer = Writer()
    client._control_sender = HTTP2Sender(writer, client._factory, 0)
    frame = client._factory.create(FrameTypes.HEADERS, 1, 1)
    frame.headers = FIELDS
    await client.send_raw_frame(frame)
    emitted = frames(writer.writes[0])
    assert len(emitted) == 1
    assert emitted[0][:3] == (1, 1, 1)
    assert len(emitted[0][3]) > 16384


@pytest.mark.asyncio
async def test_failed_fragmented_write_disables_connection():
    class FailedWriter(Writer):
        async def write(self, data):
            await super().write(data)
            raise BrokenPipeError()

    factory = FrameFactory()
    writer = FailedWriter()
    first = HTTP2Sender(writer, factory, 1)
    second = HTTP2Sender(writer, factory, 3)
    await first.send_response_headers(HTTPStatus.OK, FIELDS)
    await second.send_response_headers(HTTPStatus.OK, FIELDS)
    assert len(writer.writes) == 1
    assert writer.peer_gone


@pytest.mark.asyncio
@pytest.mark.parametrize('native', [False, True])
@pytest.mark.parametrize('buffered', [False, True])
async def test_interim_response_and_trailers_share_fragmentation(native, buffered):
    factory = FrameFactory()
    writer = Writer()
    sender = HTTP2Sender(writer, factory, 1)
    events = [
        {'type': 'http.response.start', 'status': 103, 'headers': FIELDS},
        {'type': 'http.response.body', 'body': b''},
        {'type': 'http.response.start', 'status': 200, 'headers': FIELDS, 'trailers': True},
        {'type': 'http.response.body', 'body': b'ok', 'more_body': True},
        {'type': 'http.response.trailers', 'headers': FIELDS},
    ]
    for event in events:
        await sender(_native_from_asgi(event) if native else event)
        if not buffered and sender._auto_flush_task is not None:
            await sender._auto_flush_task
    if sender._auto_flush_task is not None:
        await sender._auto_flush_task
    emitted = frames(b''.join(writer.writes))
    peer = Decoder(max_header_list_size=100000)
    sections = []
    block = b''
    for kind, flags, sid, payload in emitted:
        assert len(payload) <= 16384 and sid == 1
        if kind == 0:
            assert not block and payload == b'ok'
            continue
        assert kind == (9 if block else 1)
        block += payload
        if flags & 4:
            sections.append(peer.decode(block, raw=True))
            block = b''
    assert not block and len(sections) == 3
    assert (b':status', b'103') in sections[0]
    assert (b':status', b'200') in sections[1]
    assert all(all(field in section for field in FIELDS) for section in sections)
    assert [flags & 1 for kind, flags, _, _ in emitted if kind == 1] == [0, 0, 1]
