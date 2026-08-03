"""Sprint 90 — bounded control-frame servicing for "stuck" handlers.

With inline reading (the default), PING→PONG and CLOSE-echo happen only when
the handler calls ``receive()``.  Two new mechanisms bound that latency for
handlers that are not reading:

- **Send-time servicing**: each ``send()`` services control frames that are
  already fully buffered (non-blocking, buffered-bytes only).  Covers push
  and send-in-the-middle handlers.
- **Idle watchdog**: the per-process deadline scanner watches each connection;
  a connection quiet for more than one tick gets its buffered control frames
  serviced, bounding worst-case PONG latency to ~one scanner tick with no
  per-connection asyncio timers.

Both are driven over the real server (``NativeTestServer``) with a real
``websockets`` client.  The tick is shrunk via monkeypatch so the watchdog
tests finish quickly.
"""
import asyncio

import pytest
import websockets

import blackbull.server.deadline as _deadline

from blackbull import BlackBull, WebSocketResponse
from blackbull.testing import NativeTestServer
from blackbull.utils import Scheme


def _ws_uri(server, path: str = '/ws') -> str:
    return f'ws://127.0.0.1:{server.port}{path}'


@pytest.mark.asyncio
async def test_ping_answered_while_handler_send_only(monkeypatch):
    """A handler that sends but never receives still answers PINGs.

    Send-time servicing reads the fully-buffered PING at the next ``send()``,
    so a pusher answers keepalives without ever calling ``receive()``.
    """
    monkeypatch.setattr(_deadline, '_TICK_S', 0.05)

    app = BlackBull()

    @app.route(path='/ws', scheme=Scheme.websocket)
    async def ws_handler(conn, receive, send):
        await receive()
        await send({'type': 'websocket.accept'})
        for i in range(60):                    # send-only loop, never receives
            await send(WebSocketResponse(f'push-{i}'))
            await asyncio.sleep(0.02)

    async with NativeTestServer(app) as server:
        async with websockets.connect(_ws_uri(server)) as ws:
            # ``ws.ping()`` is a coroutine that *sends* the PING and returns
            # the shielded pong waiter — the waiter is what must be awaited.
            pong_waiter = await ws.ping()

            async def _drain():
                while True:
                    await ws.recv()

            drain = asyncio.create_task(_drain())
            try:
                await asyncio.wait_for(pong_waiter, timeout=3.0)
            finally:
                drain.cancel()


@pytest.mark.asyncio
async def test_ping_answered_for_idle_handler(monkeypatch):
    """A handler that accepts then does nothing still answers PINGs.

    The deadline-scanner watchdog services buffered control frames on a
    connection quiet for more than one tick — PONG latency bounded to ~one
    tick with no per-connection timers.
    """
    monkeypatch.setattr(_deadline, '_TICK_S', 0.05)

    app = BlackBull()

    @app.route(path='/ws', scheme=Scheme.websocket)
    async def ws_handler(conn, receive, send):
        await receive()
        await send({'type': 'websocket.accept'})
        await asyncio.sleep(1.0)               # idle — no receive, no send

    async with NativeTestServer(app) as server:
        async with websockets.connect(_ws_uri(server)) as ws:
            pong_waiter = await ws.ping()
            await asyncio.wait_for(pong_waiter, timeout=3.0)


@pytest.mark.asyncio
async def test_ping_answered_between_receives(monkeypatch):
    """A handler that does slow work between ``receive()`` calls answers PINGs
    while the handler is busy (send-time servicing on the *send* after the
    work), not only at the next receive."""
    monkeypatch.setattr(_deadline, '_TICK_S', 0.05)

    app = BlackBull()

    @app.route(path='/ws', scheme=Scheme.websocket)
    async def ws_handler(conn, receive, send):
        await receive()
        await send({'type': 'websocket.accept'})
        await receive()                        # one message
        await asyncio.sleep(0.2)               # long processing, no send
        await send(WebSocketResponse('done'))  # send resumes servicing

    async with NativeTestServer(app) as server:
        async with websockets.connect(_ws_uri(server)) as ws:
            await ws.send('work')              # let the handler start its sleep
            pong_waiter = await ws.ping()
            # Read the handler's eventual 'done' push so the connection stays
            # unblocked; the PONG must arrive well before we give up.
            await asyncio.wait_for(pong_waiter, timeout=2.0)
