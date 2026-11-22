"""Signature injection over the real stack.

``test_websocket_injection.py`` pins the plan and the wrapper against a
scripted channel. This file drives the same signatures through
:class:`~blackbull.testing.TestClient` — router, WebSocket actor, recipient,
codec, sender — so that what the handler receives is proven to come from a
real handshake and a real query string rather than a hand-built
:class:`~blackbull.connection.Connection`.

The rejection tests matter most here: a refused handshake is a *wire*
outcome, and the client has to read it as an HTTP 400 refusal.
"""
import pytest

from blackbull import BlackBull, Depends, WebSocket
from blackbull.testing import HandshakeError, TestClient, WebSocketDisconnect
from blackbull.utils import Scheme

TEARDOWNS: list[str] = []


def _make_app() -> BlackBull:
    app = BlackBull()

    @app.route(path='/rooms/{room}', scheme=Scheme.websocket)
    async def chat(ws: WebSocket, room: str, since: int = 0):
        await ws.accept()
        await ws.send_text(f'room={room} since={since}')
        await ws.close()

    @app.route(path='/seats/{seat}', scheme=Scheme.websocket)
    async def seat(ws: WebSocket, seat: int):
        await ws.accept()
        await ws.send_text(f'seat={seat} type={type(seat).__name__}')
        await ws.close()

    @app.route(path='/strict', scheme=Scheme.websocket)
    async def strict(ws: WebSocket, token: str):
        await ws.accept()
        await ws.send_text(f'token={token}')
        await ws.close()

    @app.route(path='/held', scheme=Scheme.websocket)
    async def held(ws: WebSocket, db=Depends(_provider)):
        await ws.accept()
        async for message in ws:
            await ws.send_text(f'{db}:{message}')

    return app


async def _provider():
    try:
        yield 'DB'
    finally:
        TEARDOWNS.append('teardown')


@pytest.fixture(autouse=True)
def _clear_teardowns():
    TEARDOWNS.clear()
    yield


# ---------------------------------------------------------------------------
# Injection
# ---------------------------------------------------------------------------

@pytest.mark.parametrize('url,expected', [
    pytest.param('/rooms/lobby?since=7', 'room=lobby since=7', id='path-and-query'),
    pytest.param('/rooms/lobby', 'room=lobby since=0', id='query-default'),
    pytest.param('/seats/12', 'seat=12 type=int', id='path-coerced'),
])
def test_path_and_query_params_reach_the_handler(url, expected):
    app = _make_app()
    with TestClient(app) as client:
        with client.websocket_connect(url) as ws:
            assert ws.receive_text() == expected


# ---------------------------------------------------------------------------
# Rejection reaches the client as a close code
# ---------------------------------------------------------------------------

@pytest.mark.parametrize('url', [
    pytest.param('/strict', id='missing-required-query'),
    pytest.param('/seats/front-row', id='uncoercible-path-param'),
])
def test_missing_required_query_param_is_refused_with_400(url):
    app = _make_app()
    with TestClient(app) as client:
        with pytest.raises(HandshakeError, match='HTTP 400'):
            with client.websocket_connect(url):
                pass


@pytest.mark.asyncio
async def test_a_real_client_reads_the_refusal_as_an_http_response():
    from tests.harness import served
    async with served(_make_app()) as server:
        wire = await server.wire()
        await wire.send(b'GET /strict HTTP/1.1\r\nHost: a\r\nUpgrade: websocket\r\n'
                        b'Connection: Upgrade\r\nSec-WebSocket-Version: 13\r\n'
                        b'Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n\r\n')
        reply = await wire.response()
    assert reply.status == 400 and b'token' in reply.body


# ---------------------------------------------------------------------------
# Dependency lifetime over a real socket
# ---------------------------------------------------------------------------

def test_dependency_is_live_for_the_whole_socket_and_torn_down_after_it():
    """One resolution serves every message, and release happens on disconnect.

    The teardown assertion sits *outside* the client block: the handler only
    exits when the peer goes away, which is exactly the lifetime the feature
    documents.
    """
    app = _make_app()
    with TestClient(app) as client:
        with client.websocket_connect('/held') as ws:
            ws.send_text('one')
            assert ws.receive_text() == 'DB:one'
            ws.send_text('two')
            assert ws.receive_text() == 'DB:two'
            assert TEARDOWNS == [], 'released while the socket was still open'

    assert TEARDOWNS == ['teardown']
