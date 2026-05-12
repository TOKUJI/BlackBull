"""Integration tests for advanced WebSocket protocol features.

Covers subprotocol negotiation and server-initiated close codes — protocol
details that can only be verified over a real TCP connection.
"""
import asyncio
from multiprocessing import Process

import pytest
import websockets
import websockets.exceptions

from blackbull import BlackBull, WebSocketResponse
from blackbull.utils import Scheme


def _make_app() -> BlackBull:
    app = BlackBull()

    @app.route(path='/chat', scheme=Scheme.websocket)
    async def chat(scope, receive, send):
        await receive()  # websocket.connect
        # Echo back the first requested subprotocol (or None)
        subprotocols = scope.get('subprotocols', [])
        subprotocol = subprotocols[0] if subprotocols else None
        await send({'type': 'websocket.accept', 'subprotocol': subprotocol})
        msg = await receive()
        await send(WebSocketResponse(msg.get('text', '')))
        await send({'type': 'websocket.close', 'code': 1000})

    @app.route(path='/going-away', scheme=Scheme.websocket)
    async def going_away(scope, receive, send):
        await receive()  # websocket.connect
        await send({'type': 'websocket.accept', 'subprotocol': None})
        await receive()  # wait for one message
        # Close with 1001 Going Away
        await send({'type': 'websocket.close', 'code': 1001})

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


def _ws_base(app) -> str:
    return f'ws://127.0.0.1:{app.port}'


@pytest.mark.integration
@pytest.mark.asyncio
async def test_subprotocol_negotiation(live):
    uri = f'{_ws_base(live)}/chat'
    async with websockets.connect(uri, subprotocols=['chat', 'json']) as ws:
        assert ws.subprotocol == 'chat'
        await ws.send('hello')
        reply = await ws.recv()
    assert reply == 'hello'


@pytest.mark.integration
@pytest.mark.asyncio
async def test_no_subprotocol_when_not_requested(live):
    uri = f'{_ws_base(live)}/chat'
    async with websockets.connect(uri) as ws:
        assert ws.subprotocol is None
        await ws.send('hi')
        reply = await ws.recv()
    assert reply == 'hi'


@pytest.mark.integration
@pytest.mark.asyncio
async def test_server_close_code_1001(live):
    uri = f'{_ws_base(live)}/going-away'
    close_code = None
    try:
        async with websockets.connect(uri) as ws:
            await ws.send('bye')
            # Server closes with 1001; websockets raises ConnectionClosedOK or
            # ConnectionClosed depending on the code
            await ws.recv()
    except websockets.exceptions.ConnectionClosed as exc:
        close_code = exc.rcvd.code if exc.rcvd else None

    assert close_code == 1001
