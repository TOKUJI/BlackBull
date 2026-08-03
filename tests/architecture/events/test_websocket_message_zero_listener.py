"""Sprint 90 — ``websocket_message`` zero-listener hot path.

The read-time emit adapter must skip the ``Event`` + detail-dict allocation
and the ``emit`` indirection when nobody is listening — that is the
documented purpose of ``EventAggregator.has_websocket_message_listeners``
(the per-frame check exists so a throughput workload with no observer never
pays for an event nobody receives).  The guard must therefore be re-evaluated
*per message*, not baked in at connection build time: a listener registered
after the connection exists must still receive events.
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
async def test_websocket_message_listener_registered_after_connect_still_fires():
    """A listener registered after the connection is up still receives the
    read-time event (the emit guard re-checks per message, so it must not
    be frozen to False at connection build time)."""
    app = BlackBull()
    detail: dict = {}
    fired = asyncio.Event()

    @app.route(path='/ws', scheme=Scheme.websocket)
    async def ws_handler(conn, receive, send):
        await receive()                       # websocket.connect
        await send({'type': 'websocket.accept'})
        msg = await receive()                 # consumes -> inline read path
        assert msg.get('text') == 'late-registered'
        await send({'type': 'websocket.close'})

    async with NativeTestServer(app) as server:
        async with websockets.connect(_ws_uri(server)) as ws:
            # Register the observer AFTER the recipient already exists.
            @app.on('websocket_message')
            async def observer(event):
                detail.update(event.detail)
                fired.set()
            await ws.send('late-registered')
            await asyncio.wait_for(fired.wait(), timeout=3.0)

    assert detail.get('text') == 'late-registered'
    assert detail.get('bytes') is None
