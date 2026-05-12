"""Integration tests for StreamingResponse over HTTP/1.1 plain TCP.

HTTP/2 streaming is covered by test_http2.py; this file exercises the
HTTP/1.1 chunked-transfer code path, which is separate.
"""
import asyncio
from multiprocessing import Process

import httpx
import pytest

from blackbull import BlackBull
from blackbull.response import StreamingResponse


def _make_app() -> BlackBull:
    app = BlackBull()

    @app.route(path='/stream/abc')
    async def stream_abc(scope, receive, send):
        async def chunks():
            for ch in [b'a', b'b', b'c']:
                yield ch
        await StreamingResponse(chunks())(scope, receive, send)

    @app.route(path='/stream/large')
    async def stream_large(scope, receive, send):
        chunk = b'x' * 1024

        async def chunks():
            for _ in range(1000):
                yield chunk

        await StreamingResponse(chunks())(scope, receive, send)

    @app.route(path='/stream/sse')
    async def stream_sse(scope, receive, send):
        async def events():
            yield b'data: hello\n\n'

        await StreamingResponse(
            events(),
            media_type='text/event-stream',
        )(scope, receive, send)

    return app


@pytest.fixture(scope="module")
def live():
    app = _make_app()
    app.create_server(port=0)
    p = Process(target=lambda: asyncio.run(app.run()))
    p.start()
    app.wait_for_port(timeout=10.0)
    yield app
    app.stop()
    p.terminate()
    p.join(timeout=5)


def _base(app) -> str:
    return f'http://127.0.0.1:{app.port}'


@pytest.mark.integration
@pytest.mark.asyncio
async def test_chunks_arrive_in_order(live):
    async with httpx.AsyncClient() as c:
        r = await c.get(f'{_base(live)}/stream/abc')
    assert r.status_code == 200
    assert r.content == b'abc'


@pytest.mark.integration
@pytest.mark.asyncio
async def test_large_streaming_body(live):
    async with httpx.AsyncClient() as c:
        r = await c.get(f'{_base(live)}/stream/large')
    assert r.status_code == 200
    assert len(r.content) == 1000 * 1024


@pytest.mark.integration
@pytest.mark.asyncio
async def test_custom_media_type(live):
    async with httpx.AsyncClient() as c:
        r = await c.get(f'{_base(live)}/stream/sse')
    assert r.status_code == 200
    assert 'text/event-stream' in r.headers.get('content-type', '')
