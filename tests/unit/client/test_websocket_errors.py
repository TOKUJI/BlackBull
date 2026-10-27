import asyncio
from base64 import b64encode
from contextlib import asynccontextmanager
from hashlib import sha1
from pathlib import Path
import ssl

from hpack import Encoder
import pytest

from blackbull.client import (ClientError, ProtocolError, WebSocketClient,
                              WebSocketH2Client)
from blackbull.protocol.frame_types import FrameTypes
from blackbull.server import recipient
from tests.conformance.http2._harness import _make_h2_frame
from tests.harness import Wire, peer, read_h2_frame


@asynccontextmanager
async def _session(transport, frames, replies=None):
    completed = asyncio.get_running_loop().create_future()
    completed.add_done_callback(lambda future: future.exception())
    context = client_context = None
    if transport == 'h2':
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        certs = Path(__file__).resolve().parents[2]
        context.load_cert_chain(certs / 'cert.pem', certs / 'key.pem')
        context.set_alpn_protocols(['h2'])
        client_context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        client_context.check_hostname = False
        client_context.verify_mode = ssl.CERT_NONE
        client_context.set_alpn_protocols(['h2'])

    async def origin(reader, writer):
        try:
            wire = Wire(reader, writer)
            if transport == 'h1':
                request = await wire.recv_until()
                key = next(line.split(b':', 1)[1].strip()
                           for line in request.split(b'\r\n')
                           if line.lower().startswith(b'sec-websocket-key:'))
                accept = b64encode(sha1(
                    key + b'258EAFA5-E914-47DA-95CA-C5AB0DC85B11').digest())
                await wire.send(b'HTTP/1.1 101 Switching Protocols\r\n'
                                b'Upgrade: websocket\r\nConnection: Upgrade\r\n'
                                b'Sec-WebSocket-Accept: ' + accept + b'\r\n\r\n'
                                + frames)
            else:
                assert await wire.recv_exactly(24) == b'PRI * HTTP/2.0\r\n\r\nSM\r\n\r\n'
                await wire.send(_make_h2_frame(FrameTypes.SETTINGS,
                                              payload=b'\x00\x08\x00\x00\x00\x01'))
                while True:
                    kind, flags, sid, _ = await read_h2_frame(reader)
                    if kind == 4 and not flags & 1:
                        await wire.send(_make_h2_frame(FrameTypes.SETTINGS, 1))
                    if kind == 1:
                        break
                await wire.send(_make_h2_frame(FrameTypes.HEADERS, 4, sid,
                    Encoder().encode([(b':status', b'200')]))
                    + _make_h2_frame(FrameTypes.DATA, 0, sid, frames))
            outgoing = await wire.recv_all()
            if replies is not None:
                if transport == 'h1':
                    replies.extend(outgoing)
                else:
                    while outgoing:
                        size = int.from_bytes(outgoing[:3], 'big')
                        if outgoing[3] == 0 and int.from_bytes(outgoing[5:9], 'big') == sid:
                            replies.extend(outgoing[9:9 + size])
                        outgoing = outgoing[9 + size:]
            completed.set_result(None)
        except Exception as exc:
            completed.set_exception(exc)

    client_type = WebSocketClient if transport == 'h1' else WebSocketH2Client
    async with peer(origin, ssl=context) as port:
        async with client_type('127.0.0.1', port, ssl=client_context) as client:
            session = await client.connect('/ws')
            try:
                yield session
            finally:
                await session.close(drain_timeout=0.05)
    await asyncio.wait_for(completed, 1)


async def _receive(session):
    return await asyncio.wait_for(session.receive(), 1)


def _assert_disconnect(transport, event, code):
    if transport == 'h1':
        assert event == {'type': 'websocket.disconnect', 'code': code}
    else:
        assert event == (8, code.to_bytes(2, 'big'))


@pytest.mark.asyncio
@pytest.mark.parametrize('transport', ['h1', 'h2'])
@pytest.mark.parametrize('frames,code,message', [
    pytest.param(b'\x82\x05', 1009, 'payload', id='frame-limit'),
    pytest.param(b'\x82\x03abc', 1009, 'message', id='message-limit'),
    pytest.param(b'\x02\x02ab\x80\x01c', 1009, 'message', id='fragmented-message-limit'),
    pytest.param(b'\x81\x01\xff', 1007, 'UTF-8', id='invalid-utf8'),
    pytest.param(b'\x09\x00', 1002, 'fragmented control', id='fragmented-control'),
])
async def test_receive_protocol_errors_are_client_errors(monkeypatch, transport,
                                                        frames, code, message):
    monkeypatch.setenv('BB_CLIENT_WS_MAX_FRAME_PAYLOAD', '4')
    monkeypatch.setenv('BB_CLIENT_WS_MAX_MESSAGE_SIZE', '2')
    async with _session(transport, frames) as session:
        with pytest.raises(ClientError) as caught:
            await _receive(session)
        error = caught.value
        assert type(error) is ProtocolError
        assert type(error.__cause__) is recipient.ProtocolError
        assert error.__cause__.close_code == code
        assert message.lower() in str(error).lower()
        assert str(error) == str(error.__cause__)
        _assert_disconnect(transport, await _receive(session), code)


@pytest.mark.asyncio
@pytest.mark.parametrize('transport', ['h1', 'h2'])
async def test_valid_messages_at_limits_and_close_keep_their_result(monkeypatch, transport):
    monkeypatch.setenv('BB_CLIENT_WS_MAX_FRAME_PAYLOAD', '4')
    monkeypatch.setenv('BB_CLIENT_WS_MAX_MESSAGE_SIZE', '4')
    async with _session(transport, b'\x81\x04text\x02\x02ab\x80\x02cd'
                        b'\x88\x02\x03\xe8') as session:
        text, binary = await _receive(session), await _receive(session)
        if transport == 'h1':
            assert text == {'type': 'websocket.receive', 'text': 'text', 'bytes': None}
            assert binary == {'type': 'websocket.receive', 'text': None, 'bytes': b'abcd'}
        else:
            assert text == (1, b'text')
            assert binary == (2, b'abcd')
        _assert_disconnect(transport, await _receive(session), 1000)


@pytest.mark.asyncio
@pytest.mark.parametrize('transport', ['h1', 'h2'])
async def test_receive_timeout_is_not_translated(transport):
    async with _session(transport, b'') as session:
        with pytest.raises(TimeoutError):
            if transport == 'h1':
                await asyncio.wait_for(session.receive(), 0.01)
            else:
                await session.receive(timeout=0.01)


@pytest.mark.asyncio
@pytest.mark.parametrize('transport', ['h1', 'h2'])
async def test_receive_cancellation_is_not_translated(transport):
    async with _session(transport, b'') as session:
        receiving = asyncio.create_task(session.receive())
        await asyncio.sleep(0)
        receiving.cancel()
        with pytest.raises(asyncio.CancelledError):
            await receiving


def _control_replies(data):
    replies = []
    while data:
        opcode, length = data[0] & 15, data[1] & 127
        assert data[1] & 128, 'client control replies must be masked'
        assert length <= 125
        key, payload = data[2:6], data[6:6 + length]
        assert len(key) == 4 and len(payload) == length
        replies.append((opcode, bytes(b ^ key[i % 4] for i, b in enumerate(payload))))
        data = data[6 + length:]
    return replies


@pytest.mark.asyncio
@pytest.mark.parametrize('transport', ['h1', 'h2'])
@pytest.mark.parametrize('first,payload', [(0x81, b'x'), (0x82, b'x'),
    (0x89, b'x'), (0x8a, b'x'), (0x88, b'\x03\xe8')])
@pytest.mark.parametrize('header_only', [False, True])
async def test_masked_server_frames_are_rejected_before_body(transport, first,
                                                            payload, header_only):
    frames = bytes([first, 128 | len(payload)])
    if not header_only:
        frames += b'\x00' * 4 + payload
    replies = bytearray()
    async with _session(transport, frames, replies) as session:
        with pytest.raises(ProtocolError, match='masked server frame') as error:
            await _receive(session)
        assert error.value.__cause__.close_code == 1002
        _assert_disconnect(transport, await _receive(session), 1002)
    outgoing = _control_replies(replies)
    assert outgoing and outgoing[0] == (8, b'\x03\xea')
    assert all(opcode == 8 for opcode, _ in outgoing)


@pytest.mark.asyncio
@pytest.mark.parametrize('transport', ['h1', 'h2'])
@pytest.mark.parametrize('length_code', [126, 127])
async def test_masked_server_header_does_not_wait_for_extended_length(transport, length_code):
    async with _session(transport, bytes([0x82, 128 | length_code])) as session:
        with pytest.raises(ProtocolError, match='masked server frame'):
            await _receive(session)
        _assert_disconnect(transport, await _receive(session), 1002)


@pytest.mark.asyncio
@pytest.mark.parametrize('transport', ['h1', 'h2'])
@pytest.mark.parametrize('frames,reply,code', [
    (b'\x89\x01p\x81\x01x\x88\x02\x03\xe8', (10, b'p'), 1000),
    (b'\x83\x00', (8, b'\x03\xea'), 1002),
    (b'\x82\x7e\x10\x00', (8, b'\x03\xf1'), 1009),
])
async def test_client_control_replies_keep_mask_on_every_path(monkeypatch, transport, frames,
                                                            reply, code):
    monkeypatch.setenv('BB_CLIENT_WS_MAX_FRAME_PAYLOAD', '1024')
    replies = bytearray()
    async with _session(transport, frames, replies) as session:
        if code == 1009:
            with pytest.raises(ProtocolError):
                await _receive(session)
        elif reply[0] == 10:
            event = await _receive(session)
            assert event['text'] == 'x' if transport == 'h1' else event == (1, b'x')
        _assert_disconnect(transport, await _receive(session), code)
    outgoing = _control_replies(replies)
    assert outgoing and outgoing[0] == reply
