"""Integration tests for WebSocket end-to-end over plain TCP.

Starts BlackBull in a child process and exercises the full WebSocket lifecycle
(upgrade handshake, bidirectional framing, clean close) with the `websockets`
client library.
"""
import asyncio
import json
from multiprocessing import Process

import pytest
import pytest_asyncio
import websockets

from blackbull import BlackBull, WebSocketResponse
from blackbull.utils import Scheme


def _make_echo_app() -> BlackBull:
    app = BlackBull()

    @app.route(path='/echo', scheme=Scheme.websocket)
    async def echo(scope, receive, send):
        msg = await receive()                       # websocket.connect
        await send({'type': 'websocket.accept', 'subprotocol': None})

        while True:
            msg = await receive()
            if msg.get('type') == 'websocket.disconnect':
                break
            if msg.get('text') is not None:
                await send(WebSocketResponse(msg['text']))
            elif msg.get('bytes') is not None:
                await send(WebSocketResponse(msg['bytes']))

        await send({'type': 'websocket.close'})

    return app


def _run(app):
    asyncio.run(app.run())


@pytest_asyncio.fixture
async def ws_app():
    app = _make_echo_app()
    app.create_server(port=0)

    p = Process(target=_run, args=(app,))
    p.start()
    app.wait_for_port(timeout=10.0)

    yield app

    app.stop()
    p.terminate()
    p.join(timeout=5)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_websocket_text_echo(ws_app):
    uri = f'ws://localhost:{ws_app.port}/echo'
    async with websockets.connect(uri) as ws:
        await ws.send('hello')
        reply = await ws.recv()
    assert reply == 'hello'


@pytest.mark.integration
@pytest.mark.asyncio
async def test_websocket_binary_echo(ws_app):
    uri = f'ws://localhost:{ws_app.port}/echo'
    async with websockets.connect(uri) as ws:
        await ws.send(b'\xde\xad\xbe\xef')
        reply = await ws.recv()
    assert reply == b'\xde\xad\xbe\xef'


@pytest.mark.integration
@pytest.mark.asyncio
async def test_websocket_multiple_messages(ws_app):
    uri = f'ws://localhost:{ws_app.port}/echo'
    messages = ['one', 'two', 'three']
    async with websockets.connect(uri) as ws:
        for m in messages:
            await ws.send(m)
        replies = [await ws.recv() for _ in messages]
    assert replies == messages


@pytest.mark.integration
@pytest.mark.asyncio
async def test_websocket_clean_close(ws_app):
    """Client-initiated close must complete without raising an exception."""
    uri = f'ws://localhost:{ws_app.port}/echo'
    async with websockets.connect(uri) as ws:
        await ws.send('ping')
        await ws.recv()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_websocket_permessage_deflate_negotiated(ws_app):
    """When the client offers permessage-deflate, the server must accept it
    and echo a compressed message through the same connection."""
    uri = f'ws://localhost:{ws_app.port}/echo'
    # websockets.connect enables permessage-deflate by default and refuses
    # to fall back silently — if the handshake didn't negotiate the
    # extension, the message would still pass uncompressed.  We assert the
    # negotiation explicitly via the extensions attribute.
    async with websockets.connect(uri) as ws:
        # `websockets >= 13` exposes negotiated extensions on the inner
        # protocol object; older versions had them on the connection.
        ext_objs = (
            getattr(ws, 'extensions', None)
            or getattr(getattr(ws, 'protocol', None), 'extensions', None)
            or []
        )
        ext_names = [type(e).__name__ for e in ext_objs]
        assert any('Deflate' in n for n in ext_names), (
            f'permessage-deflate must be negotiated; got extensions={ext_names}')
        msg = 'compress me ' * 100
        await ws.send(msg)
        reply = await ws.recv()
    assert reply == msg
