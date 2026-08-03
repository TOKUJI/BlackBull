"""Sprint 90 — ``websocket_message`` read-time contract on the *server* path.

The Sprint 89 review found the read-time contract was only pinned against a
direct ``WebSocketRecipient`` (with a ``dispatcher`` wired), which hid a
server-path gap: ``WebSocketActor`` builds its recipient without a dispatcher,
so ``_read_ahead_observed`` never fired on a real connection.  The contract
was, and is:

  ``websocket_message`` fires when the *server reads* a message, not when the
  handler consumes it — a handler that never calls ``receive()`` must still
  produce events, with the canonical detail shape ``{'conn', 'text', 'bytes'}``.

These tests drive the real server (``NativeTestServer`` — ConnectionActor →
HTTP1Actor upgrade → WebSocketActor → recipient) with a real ``websockets``
client so the whole path is covered, not just the recipient.
"""
import asyncio

import pytest
import websockets

from blackbull import BlackBull
from blackbull.testing import NativeTestServer
from blackbull.utils import Scheme


def _ws_uri(server, path: str = '/ws') -> str:
    return f'ws://127.0.0.1:{server.port}{path}'


@pytest.mark.asyncio
async def test_websocket_message_fires_for_non_consuming_handler():
    """A handler that never calls ``receive()`` still produces events.

    The contract is read-time, not consume-time: the server must keep reading
    (deferred reader, started by the idle watchdog) so a handler that only
    sends — or does nothing — does not silence observers.
    """
    app = BlackBull()
    details: list[dict] = []
    fired = asyncio.Event()

    @app.on('websocket_message')
    async def observer(event):
        details.append(event.detail)
        fired.set()

    @app.route(path='/ws', scheme=Scheme.websocket)
    async def ws_handler(conn, receive, send):
        await receive()                       # websocket.connect
        await send({'type': 'websocket.accept'})
        # Never consumes a message.  Bounded so the test teardown (which waits
        # for the handler to finish) stays fast — the watchdog's deferred
        # reader must fire the event well before this wakes.
        await asyncio.sleep(1.5)

    async with NativeTestServer(app) as server:
        async with websockets.connect(_ws_uri(server)) as ws:
            await ws.send('unconsumed')
            await asyncio.wait_for(fired.wait(), timeout=3.0)

    assert len(details) == 1
    assert details[0]['text'] == 'unconsumed'
    assert details[0]['bytes'] is None


@pytest.mark.asyncio
async def test_websocket_message_canonical_shape_on_server_path():
    """The server path emits the documented ``{'conn', 'text', 'bytes'}`` shape.

    The direct-recipient path and the docs agree on this shape; the server
    path historically emitted ``{'conn', 'message'}`` instead.  Unify on the
    documented shape.
    """
    app = BlackBull()
    detail: dict = {}
    fired = asyncio.Event()

    @app.on('websocket_message')
    async def observer(event):
        detail.update(event.detail)
        fired.set()

    @app.route(path='/ws', scheme=Scheme.websocket)
    async def ws_handler(conn, receive, send):
        await receive()
        await send({'type': 'websocket.accept'})
        msg = await receive()
        assert msg.get('text') == 'shape'
        await send({'type': 'websocket.close'})

    async with NativeTestServer(app) as server:
        async with websockets.connect(_ws_uri(server)) as ws:
            await ws.send('shape')
            await asyncio.wait_for(fired.wait(), timeout=2.0)

    assert set(detail) == {'conn', 'text', 'bytes'}
    assert detail['text'] == 'shape'
    assert detail['bytes'] is None


@pytest.mark.asyncio
async def test_websocket_message_fires_per_message_for_non_consuming_handler():
    """Multiple messages on a non-consuming connection produce multiple
    events in order (the deferred reader keeps running, not one-shot)."""
    app = BlackBull()
    texts: list[str] = []
    done = asyncio.Event()

    @app.on('websocket_message')
    async def observer(event):
        texts.append(event.detail['text'])
        if len(texts) >= 3:
            done.set()

    @app.route(path='/ws', scheme=Scheme.websocket)
    async def ws_handler(conn, receive, send):
        await receive()
        await send({'type': 'websocket.accept'})
        await asyncio.sleep(1.5)              # never consumes; bounded sleep

    async with NativeTestServer(app) as server:
        async with websockets.connect(_ws_uri(server)) as ws:
            for i in range(3):
                await ws.send(f'm{i}')
            await asyncio.wait_for(done.wait(), timeout=3.0)

    assert texts == ['m0', 'm1', 'm2']
