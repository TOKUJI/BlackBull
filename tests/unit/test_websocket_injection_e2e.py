"""Signature injection over the real stack (Sprint 83).

``test_websocket_injection.py`` pins the plan and the wrapper against a
scripted channel. This file drives the same signatures through
:class:`~blackbull.testing.TestClient` — router, WebSocket actor, recipient,
codec, sender — so that what the handler receives is proven to come from a
real handshake and a real query string rather than a hand-built
:class:`~blackbull.connection.Connection`.

The rejection tests matter most here: a refused handshake is a *wire*
outcome, and 1008 has to reach the client as a close code.
"""
import pytest

from blackbull import BlackBull, Depends, WebSocket
from blackbull.testing import TestClient, WebSocketDisconnect
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

def test_path_and_query_params_reach_the_handler():
    app = _make_app()
    with TestClient(app) as client:
        with client.websocket_connect('/rooms/lobby?since=7') as ws:
            assert ws.receive_text() == 'room=lobby since=7'


def test_query_param_default_applies_when_absent():
    app = _make_app()
    with TestClient(app) as client:
        with client.websocket_connect('/rooms/lobby') as ws:
            assert ws.receive_text() == 'room=lobby since=0'


def test_path_param_is_coerced_to_its_annotation():
    app = _make_app()
    with TestClient(app) as client:
        with client.websocket_connect('/seats/12') as ws:
            assert ws.receive_text() == 'seat=12 type=int'


# ---------------------------------------------------------------------------
# Rejection reaches the client as a close code
# ---------------------------------------------------------------------------

def test_missing_required_query_param_is_refused_with_1008():
    app = _make_app()
    with TestClient(app) as client:
        with pytest.raises(WebSocketDisconnect) as excinfo:
            with client.websocket_connect('/strict'):
                pass
    assert excinfo.value.code == 1008


def test_uncoercible_path_param_is_refused_with_1008():
    app = _make_app()
    with TestClient(app) as client:
        with pytest.raises(WebSocketDisconnect) as excinfo:
            with client.websocket_connect('/seats/front-row'):
                pass
    assert excinfo.value.code == 1008


# ---------------------------------------------------------------------------
# Dependency lifetime over a real socket
# ---------------------------------------------------------------------------

def test_dependency_is_live_for_the_whole_socket_and_torn_down_after_it():
    """One resolution serves every message, and release happens on disconnect.

    The teardown assertion sits *outside* the client block: the handler only
    exits when the peer goes away, which is exactly the lifetime this sprint
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
