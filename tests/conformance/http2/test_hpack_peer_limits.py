from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from hpack import Decoder
from hpack.hpack import decode_integer
import pytest

from blackbull.client.response import SettingsResponder as ClientSettings
from blackbull.protocol.frame import FrameFactory
from blackbull.protocol.frame_types import FrameTypes, HeaderFrameFlags, PseudoHeaders
from blackbull.server.response import SettingsResponder as ServerSettings
from blackbull.server.sender import build_response_headers, build_trailers


KINDS = ('response', 'nonstatic', 'headers', 'request', 'push', 'trailers', 'grpc', 'websocket')
FIELDS = [(b'x-state', b'reused-value'), (b'date', b'Sun, 11 Oct 2026 00:00:00 GMT')]


def _block(factory, kind, stream_id):
    if kind in ('response', 'nonstatic', 'grpc'):
        fields = FIELDS + ([(b'content-type', b'application/grpc')] if kind == 'grpc' else [])
        return build_response_headers(factory.encoder, stream_id,
                                      201 if kind == 'nonstatic' else 200,
                                      fields, end_stream=False)[9:]
    if kind == 'trailers':
        return build_trailers(factory.encoder, stream_id, FIELDS + [(b'grpc-status', b'0')])[9:]
    if kind == 'headers':
        pseudo = {PseudoHeaders.STATUS: '200'}
    else:
        pseudo = {PseudoHeaders.METHOD: 'CONNECT' if kind == 'websocket' else 'GET',
                  PseudoHeaders.SCHEME: 'https', PseudoHeaders.PATH: '/',
                  PseudoHeaders.AUTHORITY: 'example.com'}
        if kind == 'websocket':
            pseudo[PseudoHeaders.PROTOCOL] = 'websocket'
    if kind == 'push':
        return factory.push_promise(stream_id, stream_id + 1, pseudo, FIELDS).save()[13:]
    frame = factory.create(FrameTypes.HEADERS, HeaderFrameFlags.END_HEADERS, stream_id)
    frame.pseudo_headers = pseudo
    frame.headers = list(FIELDS)
    return frame.save()[9:]


def _responder(factory, side):
    sent = []

    async def send(frame):
        sent.append(frame.save())

    if side == 'server':
        handler = SimpleNamespace(factory=factory, _senders={}, send_frame=send,
                                  _connection_error=AsyncMock())
        return ServerSettings, handler, sent
    handler = SimpleNamespace(_factory=factory, _send_raw_frame=send,
                              _on_settings_ack=Mock(), _on_initial_window_size=Mock())
    return ClientSettings, handler, sent


def _settings(factory, sizes):
    data = b''.join(b'\x00\x01' + size.to_bytes(4, 'big') for size in sizes)
    return factory.create(FrameTypes.SETTINGS, 0, 0, data=data)


def _updates(block):
    values = []
    while block and block[0] & 0xe0 == 0x20:
        value, consumed = decode_integer(block, 5)
        values.append(value)
        block = block[consumed:]
    return values


@pytest.mark.asyncio
@pytest.mark.parametrize('side', ['server', 'client'])
@pytest.mark.parametrize('kind', KINDS)
@pytest.mark.parametrize('sizes', [(0,), (128,), (8192,), (0, 4096),
                                  (512, 128, 256), (128, 128), (128, 256, 256), (4096,), (2**32 - 1,)])
@pytest.mark.parametrize('one_frame', [False, True])
async def test_peer_limits_preserve_consecutive_blocks(side, kind, sizes, one_frame):
    factory = FrameFactory()
    peer = Decoder()
    initial = peer.decode(_block(factory, kind, 1), raw=True)
    responder, handler, sent = _responder(factory, side)
    for entries in ([sizes] if one_frame else [(size,) for size in sizes]):
        await responder(_settings(factory, entries)).respond(handler)
    assert len(sent) == (1 if one_frame else len(sizes))
    assert all(frame == b'\x00\x00\x00\x04\x01\x00\x00\x00\x00' for frame in sent)
    peer.max_allowed_table_size = sizes[-1]
    changed = []
    previous = 4096
    for size in sizes:
        size = min(size, 4096)
        if size != previous:
            changed.append(size)
        previous = size
    expected = [] if not changed else [min(changed)]
    if changed and changed[-1] != expected[0]:
        expected.append(changed[-1])
    block = _block(factory, kind, 3)
    assert _updates(block) == expected
    assert peer.decode(block, raw=True) == initial
    assert peer.header_table_size == min(sizes[-1], 4096)
    assert peer.decode(_block(factory, kind, 5), raw=True) == initial
    assert factory.decoder.header_table_size == 4096


@pytest.mark.asyncio
@pytest.mark.parametrize('side', ['server', 'client'])
async def test_limit_is_applied_before_ack_and_does_not_leak_between_connections(side):
    factory = FrameFactory()
    other = FrameFactory()
    peer = Decoder()
    responder, handler, _ = _responder(factory, side)
    emitted = []

    async def send(frame):
        emitted.append(frame.save())
        peer.max_allowed_table_size = 0
        assert peer.decode(_block(factory, 'response', 1), raw=True)

    if side == 'server':
        handler.send_frame = send
    else:
        handler._send_raw_frame = send
    await responder(_settings(factory, [0])).respond(handler)
    assert len(emitted) == 1
    assert _updates(_block(other, 'response', 1)) == []
    assert other.encoder.header_table_size == 4096


@pytest.mark.asyncio
async def test_invalid_server_settings_do_not_partially_resize_encoder():
    factory = FrameFactory()
    peer = Decoder()
    first = peer.decode(_block(factory, 'response', 1), raw=True)
    responder, handler, sent = _responder(factory, 'server')
    bad = factory.create(FrameTypes.SETTINGS, 0, 0,
                         data=b'\x00\x01\x00\x00\x00\x00\x00\x05\x00\x00\x00\x01')
    await responder(bad).respond(handler)
    handler._connection_error.assert_awaited_once()
    assert not sent
    assert _updates(_block(factory, 'response', 3)) == []
    assert peer.decode(_block(factory, 'response', 5), raw=True) == first


@pytest.mark.asyncio
@pytest.mark.parametrize('side', ['server', 'client'])
async def test_ack_does_not_discard_pending_update(side):
    factory = FrameFactory()
    responder, handler, sent = _responder(factory, side)
    await responder(_settings(factory, [0])).respond(handler)
    await responder(factory.settings(ack=True)).respond(handler)
    assert len(sent) == 1
    assert _updates(_block(factory, 'response', 1)) == [0]


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['empty', 'static', 'interim'])
async def test_pending_update_with_empty_or_static_only_fields(kind):
    factory = FrameFactory()
    responder, handler, _ = _responder(factory, 'server')
    await responder(_settings(factory, [0, 256, 128, 512])).respond(handler)
    if kind == 'static':
        frame = factory.push_promise(1, 2, {PseudoHeaders.METHOD: 'GET',
                                            PseudoHeaders.PATH: '/'}, [])
        block = frame.save()[13:]
    elif kind == 'empty':
        block = build_trailers(factory.encoder, 1, [])[9:]
    else:
        block = build_response_headers(factory.encoder, 1, 103, FIELDS, end_stream=False)[9:]
    assert _updates(block) == [0, 512]
    peer = Decoder()
    peer.max_allowed_table_size = 512
    result = peer.decode(block, raw=True)
    if kind == 'empty':
        assert result == []
    elif kind == 'static':
        assert result == [(b':method', b'GET'), (b':path', b'/')]
    else:
        assert result == [(b':status', b'103'), *FIELDS]
