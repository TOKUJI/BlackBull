import asyncio
from http import HTTPStatus

from hpack import Decoder, Encoder
import pytest

from blackbull import BlackBull
from blackbull.client.http1 import HTTP1Client
from blackbull.client.http2 import HTTP2Client
from blackbull.grpc import GrpcServiceRegistry, encode_message
from blackbull.protocol.frame_types import FrameTypes
from blackbull.response import Response
from blackbull.utils import Scheme
from tests.conformance.http2._harness import _make_h2_frame
from tests.integration.test_grpc import _serve
from tests.harness import read_h2_frame as _read


pytestmark = pytest.mark.integration
LIMITS = [(4096,), (0,), (128,), (0, 4096), (512, 128, 256), (256, 256)]


@pytest.mark.asyncio
@pytest.mark.parametrize('force_asgi', [False, True])
@pytest.mark.parametrize('client_type', [HTTP1Client, HTTP2Client])
async def test_response_fields_match_across_transports(monkeypatch, force_asgi, client_type):
    monkeypatch.setenv('BB_FORCE_ASGI_SCOPE', '1' if force_asgi else '0')
    app = BlackBull()

    @app.route(path='/{status:int}')
    async def respond(conn, status):
        return Response(b'ok', status=HTTPStatus(status), headers=[
            (b'x-state', b'reused-value'), (b'set-cookie', b'a=1'), (b'set-cookie', b'b=2')])

    async with _serve(app) as port:
        async with client_type('127.0.0.1', port) as client:
            for status in (200, 201, 200, 404, 201):
                response = await client.request('GET', f'/{status}')
                assert response.status == status
                assert response.body == b'ok'
                assert response.headers.get(b'x-state') == b'reused-value'
                assert response.headers.getlist(b'set-cookie') == [(b'set-cookie', b'a=1'), (b'set-cookie', b'b=2')]


def _settings(limits):
    return _make_h2_frame(FrameTypes.SETTINGS, payload=b''.join(
        b'\x00\x01' + size.to_bytes(4, 'big') for size in limits))


@pytest.mark.asyncio
@pytest.mark.parametrize('force_asgi', [False, True])
@pytest.mark.parametrize('kind', ['http', 'grpc', 'websocket'])
async def test_server_updates_hpack_across_live_streams(monkeypatch, force_asgi, kind):
    monkeypatch.setenv('BB_FORCE_ASGI_SCOPE', '1' if force_asgi else '0')
    monkeypatch.setenv('BB_H2_ENABLE_WEBSOCKET', '1')
    app = BlackBull()

    @app.route(path='/')
    async def http(conn):
        return 'ok'

    @app.route(path='/ws', scheme=Scheme.websocket)
    async def websocket(conn, receive, send):
        await receive()
        await send({'type': 'websocket.accept'})
        await send({'type': 'websocket.close'})

    registry = GrpcServiceRegistry()

    @registry.method('/svc/M')
    async def grpc(request, context):
        return b'ok'

    app.enable_grpc(registry)
    async with _serve(app) as port:
        reader, writer = await asyncio.open_connection('127.0.0.1', port)
        peer = Decoder()
        encoder = Encoder()
        writer.write(b'PRI * HTTP/2.0\r\n\r\nSM\r\n\r\n')
        try:
            for batch, limits in enumerate(LIMITS):
                writer.write(_settings(limits))
                await writer.drain()
                while True:
                    type_, flags, sid, payload = await _read(reader)
                    assert type_ != 7
                    if type_ == 4:
                        if flags & 1:
                            break
                        writer.write(_make_h2_frame(FrameTypes.SETTINGS, 1))
                        await writer.drain()
                peer.max_allowed_table_size = limits[-1]
                pending = set(range(batch * 6 + 1, batch * 6 + 7, 2))
                fields_by_stream = {sid: [] for sid in pending}
                for sid in sorted(pending):
                    fields = [(b':method', b'GET' if kind == 'http' else b'POST' if kind == 'grpc' else b'CONNECT'),
                              (b':scheme', b'http'), (b':authority', b'localhost'),
                              (b':path', b'/' if kind == 'http' else b'/svc/M' if kind == 'grpc' else b'/ws')]
                    if kind == 'grpc':
                        fields.append((b'content-type', b'application/grpc'))
                    elif kind == 'websocket':
                        fields.append((b':protocol', b'websocket'))
                    writer.write(_make_h2_frame(FrameTypes.HEADERS, 5 if kind == 'http' else 4,
                                               sid, encoder.encode(fields)))
                    if kind == 'grpc':
                        writer.write(_make_h2_frame(FrameTypes.DATA, 1, sid, encode_message(b'')))
                await writer.drain()
                while pending:
                    type_, flags, sid, payload = await _read(reader)
                    assert type_ not in (3, 7)
                    if type_ == 1:
                        assert flags & 4
                        fields_by_stream[sid].extend(peer.decode(payload, raw=True))
                    if type_ in (0, 1) and flags & 1:
                        pending.remove(sid)
                for fields in fields_by_stream.values():
                    assert (b':status', b'200') in fields
                    if kind == 'grpc':
                        assert (b'grpc-status', b'0') in fields
        finally:
            writer.close()
            await writer.wait_closed()


@pytest.mark.asyncio
async def test_client_updates_hpack_across_live_requests():
    ready = asyncio.Queue()

    async def peer(reader, writer):
        decoder = Decoder()
        encoder = Encoder()
        try:
            assert await reader.readexactly(24) == b'PRI * HTTP/2.0\r\n\r\nSM\r\n\r\n'
            for limits in LIMITS:
                writer.write(_settings(limits))
                await writer.drain()
                while True:
                    type_, flags, sid, payload = await _read(reader)
                    if type_ == 4:
                        if flags & 1:
                            break
                        writer.write(_make_h2_frame(FrameTypes.SETTINGS, 1))
                        await writer.drain()
                decoder.max_allowed_table_size = limits[-1]
                ready.put_nowait(True)
                received = 0
                while received < 3:
                    type_, flags, sid, payload = await _read(reader)
                    if type_ != 1:
                        continue
                    fields = decoder.decode(payload, raw=True)
                    assert (b'x-state', b'reused-value') in fields
                    assert (b':method', b'GET') in fields
                    writer.write(_make_h2_frame(FrameTypes.HEADERS, 5, sid,
                        encoder.encode([(b':status', b'200'), (b'content-length', b'0')])))
                    await writer.drain()
                    received += 1
        finally:
            writer.close()
            await writer.wait_closed()

    async with asyncio.timeout(10), asyncio.TaskGroup() as group:
        server = await asyncio.start_server(
            lambda reader, writer: group.create_task(peer(reader, writer)), '127.0.0.1', 0)
        async with server:
            port = server.sockets[0].getsockname()[1]
            async with HTTP2Client('127.0.0.1', port) as client:
                for _ in LIMITS:
                    await ready.get()
                    responses = await asyncio.gather(*(client.request(
                        'GET', '/', headers=[('x-state', 'reused-value')]) for _ in range(3)))
                    assert all(response.status == 200 for response in responses)
