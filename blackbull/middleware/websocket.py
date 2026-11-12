"""Handshake middleware for raw and object-form WebSocket handlers.

Downstream WebSocket objects adopt the recorded handshake state; do not
wait for an already-consumed websocket.connect event.
"""

from ..asgi import ASGIEvent
from ..native import NativeWSMessage, raw_receive
from ..websocket import (handshake_closed, mark_handshake_accepted,
                         mark_handshake_closed)


async def websocket(conn, receive, send, call_next):
    msg = await raw_receive(receive)()

    if msg.get('type') != ASGIEvent.WS_CONNECT:
        raise ValueError(
            f'Received Message ({msg}) does not request to open a websocket connection.'
        )

    await send(NativeWSMessage.accept())
    # Publish the handshake so a downstream WebSocket object does not wait for
    # a connect event that is already gone — without this it would read the
    # client's first *message* and mistake it for the handshake.
    mark_handshake_accepted(conn)

    await call_next(conn, receive, send)

    # The handler may have closed the connection itself (``await ws.close()``,
    # or a raw handler sending the event).  A second close frame after that is
    # redundant at best, so only close what is still open.
    if not handshake_closed(conn):
        await send(NativeWSMessage.close())
        mark_handshake_closed(conn)
