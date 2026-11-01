"""Cache and compression retain their distinct policies on the same wire shapes."""
import gzip

import pytest

from blackbull import BlackBull
from blackbull.client import HTTP2Client
from blackbull.connection import Connection
from blackbull.middleware import CORS, Cache, Compression
from blackbull.native import NativeResponse, NativeWSMessage
from tests.harness import served


_BODY = b'response content ' * 40


@pytest.mark.asyncio
@pytest.mark.parametrize('protocol', ['http1', 'http2'])
@pytest.mark.parametrize('asgi', [False, True], ids=['native', 'compat'])
@pytest.mark.parametrize('cache_first', [False, True], ids=['compress-cache', 'cache-compress'])
@pytest.mark.parametrize('shape', ['complete', 'split', 'empty', 'stream', 'trailers'])
async def test_response_shapes_through_both_middleware_orders(
        protocol, asgi, cache_first, shape, monkeypatch):
    monkeypatch.setenv('BB_FORCE_ASGI_SCOPE', str(int(asgi)))
    app = BlackBull()
    cache, compress = Cache(), Compression()
    app.use(CORS())
    for middleware in ((cache, compress) if cache_first else (compress, cache)):
        app.use(middleware)
    calls = 0

    @app.route(path='/')
    async def respond(conn, receive, send):
        nonlocal calls
        calls += 1
        headers = [(b'content-type', b'text/plain')]
        if shape in ('complete', 'empty'):
            await send(NativeResponse(header=headers, body=b'' if shape == 'empty' else _BODY))
        else:
            await send(NativeResponse(header=headers, expects_trailers=shape == 'trailers'))
            if shape == 'stream':
                await send(NativeResponse(body=_BODY[:100], more_body=True))
                await send(NativeResponse(body=_BODY[100:]))
            else:
                await send(NativeResponse(body=_BODY))
                if shape == 'trailers':
                    await send(NativeResponse(trailers=[(b'x-check', b'ok')]))

    async with served(app) as server:
        if protocol == 'http1':
            wire = await server.wire()
            responses = []
            for _ in range(2):
                await wire.send(b'GET / HTTP/1.1\r\nHost: test\r\n'
                                b'Accept-Encoding: gzip\r\nOrigin: https://test\r\n\r\n')
                responses.append(await wire.response())
        else:
            async with HTTP2Client('127.0.0.1', server.server.port) as client:
                responses = [await client.request('GET', '/', headers=[
                    (b'accept-encoding', b'gzip'), (b'origin', b'https://test')])
                    for _ in range(2)]

    assert calls == (1 if shape in ('complete', 'split', 'empty') else 2)
    for response in responses:
        headers = dict(response.headers)
        assert response.status == 200
        assert headers[b'access-control-allow-origin'] == b'*'
        assert b'accept-encoding' in headers[b'vary'].lower()
        if shape in ('complete', 'split'):
            assert headers[b'content-encoding'] == b'gzip'
            assert int(headers[b'content-length']) == len(response.body)
            assert gzip.decompress(response.body) == _BODY
            assert b'etag' in headers
        else:
            assert b'content-encoding' not in headers
            assert response.body == (b'' if shape == 'empty' else _BODY)
        assert dict(response.trailers) == ({b'x-check': b'ok'} if shape == 'trailers' else {})


@pytest.mark.asyncio
@pytest.mark.parametrize('middleware_type', [Cache, Compression])
@pytest.mark.parametrize('shape', ['header-only', 'pathsend', 'push', 'trailers', 'websocket'])
async def test_non_bufferable_events_release_held_headers_once(middleware_type, shape):
    first = NativeResponse(header=[(b'content-type', b'text/plain')])
    events = [first]
    if shape == 'pathsend':
        events.append(NativeResponse(file_path='asset.bin'))
    elif shape == 'push':
        events.append(NativeResponse(push='/asset', header=[]))
    elif shape == 'trailers':
        events.append(NativeResponse(body=b'x', trailers=[(b'x-check', b'ok')]))
    elif shape == 'websocket':
        events.append(NativeWSMessage.text_message('event'))
    conn = Connection.from_scope({'type': 'http', 'method': 'GET', 'path': '/',
                                  'headers': [(b'host', b'test'), (b'accept-encoding', b'gzip')]})
    sent = []

    async def send(event):
        sent.append(event)

    async def next_(request, receive, send):
        for event in events:
            await send(event)

    await middleware_type()(conn, None, send, next_)
    assert sent == events
