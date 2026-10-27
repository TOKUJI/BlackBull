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
async def _session(transport, frames):
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
            await wire.recv_all()
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
