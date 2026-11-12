"""The dict-API warnings on BlackBull's own server, in both scope lanes (BLA-672)."""
import warnings

import pytest

from blackbull import BlackBull, BlackBullDeprecationWarning, Response
from blackbull.client import HTTP1Client, HTTP2Client, WebSocketClient
from blackbull.env import reset_settings_cache
from blackbull.utils import Scheme
from blackbull.websocket import WebSocket
from tests.harness import served


pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


@pytest.fixture(params=[False, True], ids=['native', 'force-asgi'])
def lane(request, monkeypatch):
    monkeypatch.setenv('BB_FORCE_ASGI_SCOPE', '1' if request.param else '0')
    reset_settings_cache()
    yield request.param
    reset_settings_cache()


@pytest.fixture
def bbd():
    with warnings.catch_warnings(record=True) as record:
        warnings.simplefilter('always')
        yield lambda: sorted({str(w.message).rsplit('(', 1)[-1].rstrip(').')
                              for w in record
                              if issubclass(w.category, BlackBullDeprecationWarning)})


def _apps():
    app = BlackBull()

    async def gate(conn, receive, send, call_next):
        if conn.path == '/gated':
            await send({'type': 'http.response.start', 'status': 403, 'headers': []})
            await send({'type': 'http.response.body', 'body': b'no'})
            return
        await call_next(conn, receive, send)

    app.use(gate)

    @app.route(path='/native')
    async def native(conn, receive, send):
        await send(Response(await conn.body()))

    @app.route(path='/gated')
    async def gated(conn):
        return 'unreached'

    @app.route(path='/raw', scheme=Scheme.websocket)
    async def raw(conn, receive, send):
        await receive()
        await send({'type': 'websocket.accept'})
        event = await receive()
        await send({'type': 'websocket.send', 'text': event['text']})

    @app.route(path='/object', scheme=Scheme.websocket)
    async def obj(ws: WebSocket):
        await ws.accept()
        await ws.send_text(await ws.receive_text())

    return app


@pytest.mark.parametrize('client_type', [HTTP1Client, HTTP2Client])
async def test_a_middleware_dict_warns_and_native_traffic_does_not(lane, client_type, bbd):
    async with served(_apps()) as server:
        async with client_type('127.0.0.1', server.server.port) as client:
            native = await client.request('GET', '/native')
            assert native.status == 200
            assert bbd() == []
            gated = await client.request('GET', '/gated')

    assert gated.status == 403
    assert bbd() == ['BBD010']


async def _echo(port, path):
    async with WebSocketClient('127.0.0.1', port) as c:
        ws = await c.connect(path)
        await ws.send_text('hi')
        reply = (await ws.receive())['text']
        await ws.close()
        return reply


async def test_a_raw_websocket_handler_warns(lane, bbd):
    async with served(_apps()) as server:
        reply = await _echo(server.server.port, '/raw')

    assert reply == 'hi'
    assert bbd() == ['BBD010', 'BBD011']


async def test_the_websocket_object_does_not_warn(lane, bbd):
    async with served(_apps()) as server:
        reply = await _echo(server.server.port, '/object')

    assert reply == 'hi'
    assert bbd() == []


async def test_a_foreign_asgi_app_does_not_warn(lane, bbd):
    if not lane:
        pytest.skip('a foreign app is hosted only under BB_FORCE_ASGI_SCOPE (BLA-673)')
    async def foreign(scope, receive, send):
        if scope['type'] != 'http':
            return
        await receive()
        await send({'type': 'http.response.start', 'status': 200, 'headers': []})
        await send({'type': 'http.response.body', 'body': b'ok'})

    async with served(foreign) as server:
        async with HTTP1Client('127.0.0.1', server.server.port) as client:
            reply = await client.request('GET', '/')

    assert reply.body == b'ok'
    assert bbd() == []
