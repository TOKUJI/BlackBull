"""RFC 9110 §9.3.2: HEAD on a GET route answers as GET would, without content."""
import pytest
from httpx import ASGITransport, AsyncClient

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


@pytest.mark.parametrize('client', [HTTP1Client, HTTP2Client], ids=['http1', 'http2'])
@pytest.mark.parametrize('asgi', [None, True], ids=['default', 'asgi'])
async def test_allow_lists_the_methods_the_server_accepts(client, asgi):
    app = BlackBull()

    async def handler():
        return b'ok'

    app.route(path='/get')(handler)
    app.route(path='/explicit', methods=['GET', 'HEAD'])(handler)
    app.route(path='/post', methods=['POST'])(handler)

    async with served(app, asgi=asgi) as s:
        async with client('127.0.0.1', s.server.port) as c:
            for path in ('/get', '/explicit'):
                assert (await c.request('HEAD', path)).status == 200
                for method in ('PUT', 'OPTIONS'):
                    reply = await c.request(method, path)
                    assert reply.status == 405
                    assert sorted(reply.headers.get(b'allow').split(b', ')) == [b'GET', b'HEAD']
            reply = await c.request('OPTIONS', '/post')
            assert reply.status == 405
            assert reply.headers.get(b'allow') == b'POST'


async def test_allow_does_not_advertise_unregistered_head_on_an_external_host():
    app = BlackBull()

    @app.route(path='/get')
    async def handler():
        return b'ok'

    async with AsyncClient(transport=ASGITransport(app=app),
                           base_url='http://testserver') as client:
        head = await client.head('/get')
        assert head.status_code == 405
        assert head.headers['allow'] == 'GET'
