import asyncio

from hpack import Decoder, Encoder
import pytest

from blackbull.protocol.frame_types import FrameTypes
from blackbull.server.recipient import ProtocolError
from blackbull.server.ws_codec import encode_frame
from tests.conformance.http2._harness import _make_h2_frame, h2_connect
from tests.harness import read_h2_frame, served


async def _upgrade(server, transport):
    if transport == 'h1':
        wire = await server.wire()
        await wire.send(b'GET /ws HTTP/1.1\r\nHost: localhost\r\n'
                        b'Upgrade: websocket\r\nConnection: Upgrade\r\n'
                        b'Sec-WebSocket-Version: 13\r\n'
                        b'Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n\r\n')
        assert (await wire.response()).status == 101
    else:
        wire = await h2_connect(server)
        block = Encoder().encode([(b':method', b'CONNECT'), (b':protocol', b'websocket'),
            (b':scheme', b'http'), (b':path', b'/ws'), (b':authority', b'localhost')])
        await wire.send(_make_h2_frame(FrameTypes.HEADERS, 4, 1, block))
        while True:
            kind, _, sid, payload = await read_h2_frame(wire.reader)
            if kind == 1 and sid == 1:
                assert (b':status', b'200') in Decoder().decode(payload, raw=True)
                break
    return wire


async def _send(wire, transport, data):
    await wire.send(data if transport == 'h1' else
                    _make_h2_frame(FrameTypes.DATA, 0, 1, data))


async def _reply(wire, transport):
    if transport == 'h1':
        head = await asyncio.wait_for(wire.reader.readexactly(2), 1)
        data = head + await wire.recv_exactly(head[1] & 127)
    else:
        async with asyncio.timeout(1):
            while True:
                kind, _, sid, data = await read_h2_frame(wire.reader)
                if kind == 0 and sid == 1 and data:
                    break
    assert not data[1] & 128, 'server control and data frames must not be masked'
    assert len(data) == 2 + (data[1] & 127)
    return data[0] & 15, data[2:]


def _app(observed):
    async def app(conn, receive, send):
        await receive()
        await send({'type': 'websocket.accept'})
        try:
            while (event := await receive())['type'] != 'websocket.disconnect':
                observed.append(event)
                await send({'type': 'websocket.send', 'text': event['text']})
        except ProtocolError:
            pass
    return app


@pytest.mark.asyncio
@pytest.mark.parametrize('transport', ['h1', 'h2'])
@pytest.mark.parametrize('header', [b'\x81\x01', b'\x82\x7e', b'\x82\x7f', b'\x89\x01',
                                  b'\x8a\x01', b'\x88\x02'])
async def test_server_rejects_unmasked_header_without_waiting_for_body(monkeypatch,
                                                                     transport, header):
    monkeypatch.setenv('BB_H2_ENABLE_WEBSOCKET', '1')
    observed = []
    async with served(_app(observed)) as server:
        wire = await _upgrade(server, transport)
        await _send(wire, transport, header)
        assert await _reply(wire, transport) == (8, b'\x03\xea')
        assert not observed


@pytest.mark.asyncio
@pytest.mark.parametrize('transport', ['h1', 'h2'])
async def test_server_legal_masked_frames_keep_control_and_data_unmasked(monkeypatch, transport):
    monkeypatch.setenv('BB_H2_ENABLE_WEBSOCKET', '1')
    observed = []
    async with served(_app(observed)) as server:
        wire = await _upgrade(server, transport)
        await _send(wire, transport, encode_frame(b'p', opcode=9, mask=True)
                    + encode_frame(b'x', opcode=1, mask=True))
        assert await _reply(wire, transport) == (10, b'p')
        assert await _reply(wire, transport) == (1, b'x')
        assert len(observed) == 1 and observed[0]['text'] == 'x'
        await _send(wire, transport, encode_frame(b'\x03\xe8', opcode=8, mask=True))
        assert await _reply(wire, transport) == (8, b'\x03\xe8')


@pytest.mark.asyncio
@pytest.mark.parametrize('transport', ['h1', 'h2'])
@pytest.mark.parametrize('data,code', [(b'\x83\x80\0\0\0\0', 1002),
                                     (b'\x09\x80', 1002),
                                     (b'\x82\xfe\x10\x00', 1009)])
async def test_server_error_closes_are_unmasked(monkeypatch, transport, data, code):
    monkeypatch.setenv('BB_H2_ENABLE_WEBSOCKET', '1')
    monkeypatch.setenv('BB_WS_MAX_FRAME_PAYLOAD', '4')
    observed = []
    async with served(_app(observed)) as server:
        wire = await _upgrade(server, transport)
        await _send(wire, transport, data)
        assert await _reply(wire, transport) == (8, code.to_bytes(2, 'big'))
        assert not observed
