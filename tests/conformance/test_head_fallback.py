"""RFC 9110 §9.3.2: HEAD on a GET route answers as GET would, without content."""
import pytest

from blackbull import BlackBull, Response
from blackbull.client.http1 import HTTP1Client
from blackbull.client.http2 import HTTP2Client

from tests.harness import served

pytestmark = pytest.mark.asyncio


@pytest.mark.parametrize('client', [HTTP1Client, HTTP2Client], ids=['http1', 'http2'])
async def test_head_on_a_get_route_answers_like_get_without_content(client):
    app = BlackBull()

    @app.route(path='/get')
    async def get(conn):
        return Response('hello')

    async with served(app) as s:
        async with client('127.0.0.1', s.server.port) as c:
            got = await c.request('GET', '/get')
            head = await c.request('HEAD', '/get')
    assert (head.status, head.body) == (200, b'')
    assert head.headers.get(b'content-length') == got.headers.get(b'content-length')
