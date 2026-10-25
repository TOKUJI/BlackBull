import asyncio
from pathlib import Path
import ssl

from hpack import Decoder, Encoder
import pytest

from blackbull.client import HandshakeError, WebSocketH2Client
from blackbull.protocol.frame_types import FrameTypes
from tests.conformance.http2._harness import _make_h2_frame
from tests.harness import Wire, peer, read_h2_frame


@pytest.mark.asyncio
@pytest.mark.parametrize('queued_data', [False, True])
@pytest.mark.parametrize('fields', [
    [(b'sec-websocket-protocol', b'chat')],
    [(b'sec-websocket-protocol', b'')],
    [(b'sec-websocket-protocol', b'chat')] * 2,
    [(b'sec-websocket-extensions', b'permessage-deflate')],
    [(b'sec-websocket-extensions', b'')],
    [(b'sec-websocket-extensions', b'permessage-deflate')] * 2,
    [(b'x-before', b'1'), (b'sec-websocket-protocol', b'chat'), (b'x-after', b'2')],
    [(b'x-before', b'1'), (b'sec-websocket-extensions', b''), (b'x-after', b'2')],
])
async def test_unoffered_negotiation_resets_stream_and_keeps_connection(fields, queued_data):
    alive = asyncio.Event()
    completed = asyncio.get_running_loop().create_future()
    completed.add_done_callback(lambda future: future.exception())

    async def origin(reader, writer):
        try:
            wire = Wire(reader, writer)
            assert await wire.recv_exactly(24) == b'PRI * HTTP/2.0\r\n\r\nSM\r\n\r\n'
            await wire.send(_make_h2_frame(FrameTypes.SETTINGS,
                                          payload=b'\x00\x08\x00\x00\x00\x01'))
            while True:
                kind, flags, sid, payload = await read_h2_frame(reader)
                if kind == 4 and not flags & 1:
                    await wire.send(_make_h2_frame(FrameTypes.SETTINGS, 1))
                if kind == 1:
                    request = Decoder().decode(payload, raw=True)
                    assert (b':method', b'CONNECT') in request
                    assert (b':protocol', b'websocket') in request
                    assert not any(k.startswith(b'sec-websocket-') for k, _ in request)
                    break
            response = _make_h2_frame(FrameTypes.HEADERS, 4, sid,
                Encoder().encode([(b':status', b'200'), *fields]))
            if queued_data:
                response += _make_h2_frame(FrameTypes.DATA, 8, sid,
                    b'\x03' + b'x' * 11996 + b'\x00' * 3) * 3
            await wire.send(response)
            reset, credit = False, 0
            while not reset or (queued_data and credit < 36000):
                kind, flags, stream, payload = await read_h2_frame(reader)
                if kind == 3:
                    assert stream == sid and int.from_bytes(payload, 'big') == 1
                    reset = True
                if kind == 8 and stream == 0:
                    credit += int.from_bytes(payload, 'big')
                assert kind != 7
            await wire.send(_make_h2_frame(FrameTypes.PING, payload=b'alive123'))
            while True:
                kind, flags, _, payload = await read_h2_frame(reader)
                assert kind != 7
                if kind == 6 and flags & 1:
                    assert payload == b'alive123'
                    alive.set()
                    break
            await wire.recv_all()
            completed.set_result(None)
        except BaseException as exc:
            completed.set_exception(exc)

    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    certs = Path(__file__).resolve().parents[2]
    context.load_cert_chain(certs / 'cert.pem', certs / 'key.pem')
    context.set_alpn_protocols(['h2'])
    client_context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    client_context.check_hostname = False
    client_context.verify_mode = ssl.CERT_NONE
    client_context.set_alpn_protocols(['h2'])
    async with peer(origin, ssl=context) as port:
        async with WebSocketH2Client('127.0.0.1', port, ssl=client_context) as client:
            with pytest.raises(HandshakeError):
                await client.connect('/ws')
            await asyncio.wait_for(alive.wait(), 1)
    await completed
