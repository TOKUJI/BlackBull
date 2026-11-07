import gzip
from http import HTTPStatus

import pytest

from blackbull import BlackBull, Response
from blackbull.client import HTTP1Client, HTTP2Client
from blackbull.env import reset_settings_cache
from blackbull.grpc import GrpcServiceRegistry, encode_message
from blackbull.middleware.compression import Compression
from tests.harness import served


pytestmark = pytest.mark.integration


@pytest.mark.asyncio
@pytest.mark.parametrize('force_asgi', [False, True])
@pytest.mark.parametrize('client_type', [HTTP1Client, HTTP2Client])
@pytest.mark.parametrize('source', ['handler', 'static'])
@pytest.mark.parametrize('codec', ['gzip', 'br'])
async def test_range_then_full_response(
        monkeypatch, tmp_path, force_asgi, client_type, source, codec):
    decoder = gzip.decompress if codec == 'gzip' else pytest.importorskip('brotli').decompress
    monkeypatch.setenv('BB_FORCE_ASGI_SCOPE', '1' if force_asgi else '0')
    reset_settings_cache()
    body = b'0123456789' * 100
    (tmp_path / 'asset.txt').write_bytes(body)
    app = BlackBull()
    app.use(Compression(min_size=1))
    app.static('/static', str(tmp_path))

    @app.route(path='/handler')
    async def handler(conn):
        if conn.headers.get(b'range'):
            return Response(body[:10], status=HTTPStatus.PARTIAL_CONTENT, content_type='text/plain',
                            headers=[(b'content-range', b'bytes 0-9/1000'),
                                     (b'content-length', b'10')])
        return Response(body, content_type='text/plain')

    path = '/handler' if source == 'handler' else '/static/asset.txt'
    headers = [(b'accept-encoding', codec.encode())]
    try:
        async with served(app) as server:
            async with client_type('127.0.0.1', server.server.port) as client:
                for ranged in (True, False, True):
                    reply = await client.request('GET', path, headers=headers + (
                        [(b'range', b'bytes=0-9')] if ranged else []))
                    if ranged:
                        assert reply.status == 206
                        assert reply.headers.get(b'content-range') == b'bytes 0-9/1000'
                        assert b'content-encoding' not in reply.headers
                        assert reply.headers.get(b'content-length') == b'10'
                        assert reply.body == body[:10]
                    else:
                        assert reply.status == 200
                        assert reply.headers.get(b'content-encoding') == codec.encode()
                        assert int(reply.headers.get(b'content-length')) == len(reply.body)
                        assert decoder(reply.body) == body
    finally:
        reset_settings_cache()


@pytest.mark.asyncio
@pytest.mark.parametrize('force_asgi', [False, True])
@pytest.mark.parametrize('shape', ['unary', 'server-streaming', 'client-streaming', 'bidi'])
async def test_compression_preserves_grpc_framing_and_trailers(monkeypatch, force_asgi, shape):
    monkeypatch.setenv('BB_FORCE_ASGI_SCOPE', '1' if force_asgi else '0')
    reset_settings_cache()
    app = BlackBull()
    app.use(Compression(min_size=1))
    registry = GrpcServiceRegistry()

    async def unary(request, context):
        return request

    async def server_stream(request, context):
        yield request
        yield request

    async def client_stream(request_iter, context):
        return b''.join([message async for message in request_iter])

    async def bidi(request_iter, context):
        async for message in request_iter:
            yield message

    handlers = {'unary': unary, 'server-streaming': server_stream,
                'client-streaming': client_stream, 'bidi': bidi}
    registry.add_method('/svc/Echo', handlers[shape],
                        client_streaming=shape in ('client-streaming', 'bidi'))
    app.enable_grpc(registry)
    message = b'x' * 256
    request = encode_message(message) * (2 if shape in ('client-streaming', 'bidi') else 1)
    expected = (encode_message(message * 2) if shape == 'client-streaming' else
                encode_message(message) * (1 if shape == 'unary' else 2))
    try:
        async with served(app) as server:
            async with HTTP2Client('127.0.0.1', server.server.port) as client:
                for _ in range(2):
                    reply = await client.request('POST', '/svc/Echo', body=request,
                        headers=[(b'content-type', b'application/grpc'),
                                 (b'accept-encoding', b'gzip')])
                    assert reply.status == 200
                    assert b'content-encoding' not in reply.headers
                    assert reply.body == expected
                    assert reply.trailers.get(b'grpc-status') == b'0'
    finally:
        reset_settings_cache()
