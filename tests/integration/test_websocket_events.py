"""Integration tests for WebSocket event hooks (guide.md §9).

websocket_connected, websocket_message, and websocket_disconnected all
fire during the WebSocket lifecycle. Each event increments a module-level
counter that the test reads back via a /ws-state HTTP endpoint.
"""
import asyncio
import time
from multiprocessing import Process

import httpx
import pytest
import websockets

from blackbull import BlackBull, WebSocketResponse
from blackbull.utils import Scheme


def _make_app() -> BlackBull:
    app = BlackBull()

    _state = {
        'connected':    0,
        'messages':     0,
        'disconnected': 0,
    }

    @app.on('websocket_connected')
    async def on_connected(event):
        _state['connected'] += 1

    @app.on('websocket_message')
    async def on_message(event):
        _state['messages'] += 1

    @app.on('websocket_disconnected')
    async def on_disconnected(event):
        _state['disconnected'] += 1

    @app.route(path='/ws', scheme=Scheme.websocket)
    async def ws_echo(scope, receive, send):
        await receive()                                         # websocket.connect
        await send({'type': 'websocket.accept', 'subprotocol': None})
        while True:
            msg = await receive()
            if msg.get('type') == 'websocket.disconnect':
                break
            if msg.get('text') is not None:
                await send(WebSocketResponse(msg['text']))
        await send({'type': 'websocket.close'})

    @app.route(path='/ws-state')
    async def ws_state():
        return dict(_state)

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


def _base_http(app) -> str:
    return f'http://127.0.0.1:{app.port}'


def _base_ws(app) -> str:
    return f'ws://127.0.0.1:{app.port}'


def _get_ws_state(base: str) -> dict:
    """Poll the /ws-state endpoint until disconnected >= 1 or timeout."""
    import httpx as _httpx
    deadline = time.monotonic() + 3.0
    last = {}
    while time.monotonic() < deadline:
        r = _httpx.get(f'{base}/ws-state')
        last = r.json()
        if last.get('disconnected', 0) >= 1:
            return last
        time.sleep(0.05)
    return last


@pytest.mark.integration
@pytest.mark.asyncio
async def test_websocket_connected_fires(live):
    async with websockets.connect(f'{_base_ws(live)}/ws') as ws:
        await ws.send('hello')
        await ws.recv()
    state = _get_ws_state(_base_http(live))
    assert state['connected'] >= 1


@pytest.mark.integration
@pytest.mark.asyncio
async def test_websocket_message_fires(live):
    async with websockets.connect(f'{_base_ws(live)}/ws') as ws:
        await ws.send('ping')
        await ws.recv()
        await ws.send('pong')
        await ws.recv()
    state = _get_ws_state(_base_http(live))
    assert state['messages'] >= 2


@pytest.mark.integration
@pytest.mark.asyncio
async def test_websocket_disconnected_fires(live):
    async with websockets.connect(f'{_base_ws(live)}/ws') as ws:
        await ws.send('bye')
        await ws.recv()
    state = _get_ws_state(_base_http(live))
    assert state['disconnected'] >= 1
