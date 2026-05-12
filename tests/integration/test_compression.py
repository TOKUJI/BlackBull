"""Integration tests for CompressionMiddleware — Accept-Encoding negotiation over TCP.

Content-Encoding negotiation is invisible to unit tests; these are the first
tests that send a real Accept-Encoding header over a TCP connection.
"""
import asyncio
import gzip
from multiprocessing import Process

import httpx
import pytest

from blackbull import BlackBull
from blackbull.middleware.compression import CompressionMiddleware


_LARGE_BODY = b'x' * 500   # > 100-byte threshold


def _make_app() -> BlackBull:
    app = BlackBull()

    compress = CompressionMiddleware()

    @app.route(path='/large', middlewares=[compress])
    async def large(scope, receive, send):
        await send({
            'type': 'http.response.start',
            'status': 200,
            'headers': [(b'content-type', b'text/plain')],
        })
        await send({'type': 'http.response.body', 'body': _LARGE_BODY, 'more_body': False})

    @app.route(path='/small', middlewares=[compress])
    async def small(scope, receive, send):
        # Body is only 5 bytes — below the 100-byte threshold
        await send({
            'type': 'http.response.start',
            'status': 200,
            'headers': [(b'content-type', b'text/plain')],
        })
        await send({'type': 'http.response.body', 'body': b'hello', 'more_body': False})

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
async def test_gzip_encoding(live):
    # httpx decompresses gzip automatically; r.content is already the raw body.
    # We verify the wire encoding by checking the response header.
    async with httpx.AsyncClient() as c:
        r = await c.get(
            f'{_base(live)}/large',
            headers={'Accept-Encoding': 'gzip'},
        )
    assert r.status_code == 200
    assert r.headers.get('content-encoding') == 'gzip'
    assert r.content == _LARGE_BODY


@pytest.mark.integration
@pytest.mark.asyncio
async def test_no_accept_encoding_returns_uncompressed(live):
    async with httpx.AsyncClient(headers={'Accept-Encoding': 'identity'}) as c:
        r = await c.get(f'{_base(live)}/large')
    assert r.status_code == 200
    assert 'content-encoding' not in r.headers
    assert r.content == _LARGE_BODY


@pytest.mark.integration
@pytest.mark.asyncio
async def test_below_threshold_not_compressed(live):
    async with httpx.AsyncClient() as c:
        r = await c.get(
            f'{_base(live)}/small',
            headers={'Accept-Encoding': 'gzip'},
        )
    assert r.status_code == 200
    assert 'content-encoding' not in r.headers
    assert r.content == b'hello'
