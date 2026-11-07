"""RFC 9110 §15.4.5: a 304 from ``Cache`` carries the stored 200's metadata."""
from email.utils import formatdate

import pytest

from blackbull import BlackBull
from blackbull.client.http1 import HTTP1Client
from blackbull.client.http2 import HTTP2Client
from blackbull.middleware.cache import Cache
from blackbull.middleware.compression import Compression
from blackbull.middleware.cors import CORS

from tests.harness import served

pytestmark = pytest.mark.asyncio

EXPIRES = formatdate(2_000_000_000, usegmt=True).encode()
REQUEST = [(b'origin', b'http://a.example'), (b'accept-encoding', b'gzip')]

ORDERS = {
    'cache-outer': lambda: (Cache(max_age=60), Compression(), CORS()),
    'cors-outer': lambda: (CORS(), Compression(), Cache(max_age=60)),
}


def _app(order) -> BlackBull:
    app = BlackBull()
    for middleware in ORDERS[order]():
        app.use(middleware)

    @app.route(path='/x')
    async def x(conn, receive, send):
        await send({'type': 'http.response.start', 'status': 200,
                    'headers': [(b'content-type', b'text/plain'),
                                (b'cache-control', b'max-age=60'),
                                (b'expires', EXPIRES)]})
        await send({'type': 'http.response.body', 'body': b'x' * 4096})

    return app


def _tokens(headers, name) -> set[bytes]:
    return {t.strip().lower() for _, v in headers.getlist(name) for t in v.split(b',')}


@pytest.mark.parametrize('order', ORDERS)
@pytest.mark.parametrize('client', [HTTP1Client, HTTP2Client], ids=['http1', 'http2'])
async def test_a_304_carries_the_stored_200_metadata(order, client):
    async with served(_app(order)) as s:
        async with client('127.0.0.1', s.server.port) as c:
            first = await c.request('GET', '/x', headers=REQUEST)
            again = await c.request('GET', '/x', headers=[
                *REQUEST, (b'if-none-match', first.headers.get(b'etag'))])
    assert (first.status, again.status) == (200, 304)
    for name in (b'cache-control', b'expires', b'etag', b'access-control-allow-origin'):
        assert again.headers.get(name) == first.headers.get(name), name
    assert _tokens(again.headers, b'vary') == _tokens(first.headers, b'vary') != set()
    assert b'content-encoding' not in again.headers
    assert b'content-type' not in again.headers
