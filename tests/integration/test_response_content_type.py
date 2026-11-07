import gzip

import pytest

from blackbull import (BlackBull, EventSourceResponse, JSONResponse, RedirectResponse,
                       Response, StreamingResponse)
from blackbull.client import HTTP1Client, HTTP2Client
from blackbull.env import reset_settings_cache
from blackbull.middleware.compression import Compression
from tests.harness import served


pytestmark = pytest.mark.integration


@pytest.mark.asyncio
@pytest.mark.parametrize('force_asgi', [False, True])
@pytest.mark.parametrize('client_type', [HTTP1Client, HTTP2Client])
@pytest.mark.parametrize('compress', [False, True])
@pytest.mark.parametrize('kind', ['response', 'json', 'redirect', 'stream', 'sse'])
async def test_one_explicit_content_type_on_wire(monkeypatch, force_asgi, client_type, compress, kind):
    monkeypatch.setenv('BB_FORCE_ASGI_SCOPE', '1' if force_asgi else '0')
    reset_settings_cache()
    app = BlackBull()
    if compress:
        app.use(Compression(min_size=1))

    async def chunks():
        yield b'hello'

    @app.route(path='/data')
    async def data(conn):
        headers = [(b'Content-Type', b'application/custom'),
                   (b'set-cookie', b'a=1'), (b'set-cookie', b'b=2')]
        if kind == 'response':
            return Response(b'hello', content_type='application/factory', headers=headers)
        if kind == 'json':
            return JSONResponse({'ok': True}, headers=headers)
        if kind == 'redirect':
            return RedirectResponse('/target', headers=headers)
        if kind == 'stream':
            return StreamingResponse(chunks(), media_type='application/factory', headers=headers)
        return EventSourceResponse(chunks(), headers=headers)

    expected = {'response': b'hello', 'json': b'{"ok": true}', 'redirect': b'',
                'stream': b'hello', 'sse': b'data: hello\n\n'}[kind]
    try:
        async with served(app) as server:
            async with client_type('127.0.0.1', server.server.port) as client:
                for _ in range(2):
                    reply = await client.request('GET', '/data',
                        headers=[(b'accept-encoding', b'gzip')])
                    assert reply.status == (302 if kind == 'redirect' else 200)
                    assert reply.headers.getlist(b'content-type') == [
                        (b'content-type', b'application/custom')]
                    assert reply.headers.getlist(b'set-cookie') == [
                        (b'set-cookie', b'a=1'), (b'set-cookie', b'b=2')]
                    body = (gzip.decompress(reply.body)
                            if reply.headers.get(b'content-encoding') == b'gzip' else reply.body)
                    assert body == expected
    finally:
        reset_settings_cache()
