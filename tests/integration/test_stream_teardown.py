"""A client that drops the connection mid-stream ends the streaming handler.

HTTP/2 can deliver nothing more once the connection is gone, so an unanswered
stream is released as a peer reset releases it; HTTP/1.1 learns it at the next
write.  Either way the handler's ``finally`` runs and no task is left behind.
"""
import asyncio

import pytest
from hpack import Encoder

from blackbull import BlackBull, StreamingResponse
from blackbull.grpc import GrpcServiceRegistry, encode_message
from tests.harness import served

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


def _frame(kind: int, flags: int, stream_id: int, payload: bytes = b'') -> bytes:
    return (len(payload).to_bytes(3, 'big') + bytes([kind, flags])
            + stream_id.to_bytes(4, 'big') + payload)


async def _open_h2(port: int, path: bytes, grpc: bool):
    reader, writer = await asyncio.open_connection('127.0.0.1', port)
    fields = [(b':method', b'POST' if grpc else b'GET'), (b':path', path),
              (b':scheme', b'http'), (b':authority', b'x')]
    if grpc:
        fields += [(b'content-type', b'application/grpc'), (b'te', b'trailers')]
    writer.write(b'PRI * HTTP/2.0\r\n\r\nSM\r\n\r\n' + _frame(4, 0, 0)
                 + _frame(1, 4 if grpc else 5, 1, Encoder().encode(fields)))
    if grpc:
        writer.write(_frame(0, 1, 1, encode_message(b'')))
    while True:                               # until the first DATA frame
        head = await asyncio.wait_for(reader.readexactly(9), 5)
        await reader.readexactly(int.from_bytes(head[:3], 'big'))
        if head[3] == 4 and not head[4] & 1:
            writer.write(_frame(4, 1, 0))
        if head[3] == 0:
            return writer


async def _open_h1(port: int, path: bytes):
    reader, writer = await asyncio.open_connection('127.0.0.1', port)
    writer.write(b'GET ' + path + b' HTTP/1.1\r\nHost: x\r\n\r\n')
    await asyncio.wait_for(reader.readuntil(b'\r\n\r\n'), 5)
    await asyncio.wait_for(reader.read(1), 5)
    return writer


def _streaming_app(closed: list):
    app = BlackBull()
    registry = GrpcServiceRegistry()
    app.enable_grpc(registry)

    @registry.method('/s/S/Stream', streaming=True)
    async def stream(request, context):
        try:
            yield encode_message(b'x')
            while True:
                await asyncio.sleep(0.05)
                yield encode_message(b'y')
        finally:
            closed.append('grpc')

    async def chunks():
        try:
            yield b'x'
            while True:
                await asyncio.sleep(0.05)
                yield b'y'
        finally:
            closed.append('http')

    @app.route(path='/stream')
    async def http_stream(conn):
        return StreamingResponse(chunks())

    return app


@pytest.mark.timeout(30)
@pytest.mark.parametrize('open_stream', [
    lambda port: _open_h2(port, b'/s/S/Stream', grpc=True),
    lambda port: _open_h2(port, b'/stream', grpc=False),
    lambda port: _open_h1(port, b'/stream'),
], ids=['grpc-h2c', 'http-h2c', 'http-h1'])
async def test_dropping_the_connection_ends_the_streaming_handler(open_stream):
    closed: list = []
    async with served(_streaming_app(closed)) as s:
        before = len(asyncio.all_tasks())
        writer = await open_stream(s.server.port)
        writer.close()
        for _ in range(100):
            if closed and len(asyncio.all_tasks()) <= before:
                break
            await asyncio.sleep(0.05)
        assert closed, 'the handler never saw the client leave'
        assert len(asyncio.all_tasks()) <= before, 'the stream left tasks behind'
