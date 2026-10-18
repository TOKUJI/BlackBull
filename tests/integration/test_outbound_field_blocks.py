import asyncio

from hpack import Decoder, Encoder
import pytest

from blackbull import BlackBull
from blackbull.client.http2 import HTTP2Client
from blackbull.grpc import GrpcServiceRegistry, encode_message
from blackbull.protocol.frame_types import FrameTypes
from blackbull.response import Response
from blackbull.utils import Scheme
from tests.conformance.http2._harness import _make_h2_frame
from tests.integration.test_grpc import _serve
from tests.integration.test_hpack_peer_limits import _read


pytestmark = pytest.mark.integration
FIELD = (b'x-large', b'~' * 25000)


async def _field_section(reader, opening, limit):
    kind, flags, sid, payload = opening
    assert kind == 1 and len(payload) <= limit
    block = [payload]
    while not flags & 4:
        kind, flags, stream, payload = await _read(reader)
        assert kind == 9 and stream == sid and len(payload) <= limit
        assert flags in (0, 4)
        block.append(payload)
    return b''.join(block)


@pytest.mark.asyncio
@pytest.mark.parametrize('force_asgi', [False, True])
@pytest.mark.parametrize('kind', ['http', 'grpc', 'websocket'])
async def test_live_server_fragments_field_sections(monkeypatch, force_asgi, kind):
    monkeypatch.setenv('BB_FORCE_ASGI_SCOPE', '1' if force_asgi else '0')
    monkeypatch.setenv('BB_H2_ENABLE_WEBSOCKET', '1')
    app = BlackBull()

    @app.route(path='/')
    async def http(conn):
        return Response(b'ok', headers=[FIELD])

    @app.route(path='/ws', scheme=Scheme.websocket)
    async def websocket(conn, receive, send):
        await receive()
        await send({'type': 'websocket.accept', 'headers': [FIELD]})
        await send({'type': 'websocket.close'})

    registry = GrpcServiceRegistry()

    @registry.method('/svc/M')
    async def grpc(request, context):
        await context.send_initial_metadata([FIELD])
        context.set_trailing_metadata([FIELD])
        return b'ok'

    app.enable_grpc(registry)
    async with _serve(app) as port:
        reader, writer = await asyncio.open_connection('127.0.0.1', port)
        peer = Decoder(max_header_list_size=100000)
        encoder = Encoder()
        writer.write(b'PRI * HTTP/2.0\r\n\r\nSM\r\n\r\n')
        try:
            for batch, limit in enumerate((16384, 32768, 16384)):
                writer.write(_make_h2_frame(FrameTypes.SETTINGS, payload=b'\x00\x05' + limit.to_bytes(4, 'big')))
                await writer.drain()
                while True:
                    type_, flags, _, _ = await _read(reader)
                    assert type_ != 7
                    if type_ == 4:
                        if flags & 1:
                            break
                        writer.write(_make_h2_frame(FrameTypes.SETTINGS, 1))
                sid = batch * 2 + 1
                fields = [(b':method', b'GET' if kind == 'http' else b'POST' if kind == 'grpc' else b'CONNECT'),
                          (b':scheme', b'http'), (b':authority', b'localhost'),
                          (b':path', b'/' if kind == 'http' else b'/svc/M' if kind == 'grpc' else b'/ws')]
                if kind == 'grpc':
                    fields.append((b'content-type', b'application/grpc'))
                elif kind == 'websocket':
                    fields.append((b':protocol', b'websocket'))
                writer.write(_make_h2_frame(FrameTypes.HEADERS, 5 if kind == 'http' else 4, sid, encoder.encode(fields)))
                if kind == 'grpc':
                    writer.write(_make_h2_frame(FrameTypes.DATA, 1, sid, encode_message(b'')))
                await writer.drain()
                sections = []
                while True:
                    opening = await _read(reader)
                    type_, flags, stream, _ = opening
                    assert type_ not in (3, 7)
                    if type_ == 1:
                        block = await _field_section(reader, opening, limit)
                        assert len(block) > limit
                        sections.append(peer.decode(block, raw=True))
                    if stream == sid and type_ in (0, 1) and flags & 1:
                        break
                assert FIELD in sections[0]
                assert (b':status', b'200') in sections[0]
                if kind == 'grpc':
                    assert FIELD in sections[-1]
                    assert (b'grpc-status', b'0') in sections[-1]
        finally:
            writer.close()
            await writer.wait_closed()


@pytest.mark.asyncio
async def test_live_client_fragments_requests_after_settings_changes():
    ready = asyncio.Queue()

    async def peer(reader, writer):
        decoder = Decoder(max_header_list_size=100000)
        encoder = Encoder()
        try:
            assert await reader.readexactly(24) == b'PRI * HTTP/2.0\r\n\r\nSM\r\n\r\n'
            for limit in (16384, 32768, 16384):
                writer.write(_make_h2_frame(FrameTypes.SETTINGS, payload=b'\x00\x05' + limit.to_bytes(4, 'big')))
                await writer.drain()
                while True:
                    type_, flags, _, _ = await _read(reader)
                    if type_ == 4:
                        if flags & 1:
                            break
                        writer.write(_make_h2_frame(FrameTypes.SETTINGS, 1))
                ready.put_nowait(True)
                for _ in range(2):
                    opening = await _read(reader)
                    assert opening[1] & 1
                    block = await _field_section(reader, opening, limit)
                    assert FIELD in decoder.decode(block, raw=True)
                    writer.write(_make_h2_frame(FrameTypes.HEADERS, 5, opening[2],
                        encoder.encode([(b':status', b'200')])))
                await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    async with asyncio.timeout(15), asyncio.TaskGroup() as group:
        server = await asyncio.start_server(
            lambda reader, writer: group.create_task(peer(reader, writer)), '127.0.0.1', 0)
        async with server:
            port = server.sockets[0].getsockname()[1]
            async with HTTP2Client('127.0.0.1', port) as client:
                for _ in range(3):
                    await ready.get()
                    responses = await asyncio.gather(*(client.request('GET', '/', headers=[FIELD]) for _ in range(2)))
                    assert all(response.status == 200 for response in responses)
