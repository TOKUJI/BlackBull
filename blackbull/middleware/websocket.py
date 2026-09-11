"""Handshake middleware — accepts the connection before the handler runs.

Strips the accept/close boilerplate from a raw-triplet handler.  It works
with either handler form: the handshake is recorded on the connection, so a
:class:`~blackbull.websocket.WebSocket` built downstream adopts that state
instead of waiting for a ``websocket.connect`` already consumed.

The object form makes it redundant — ``await ws.accept()`` is the line it
removes — but the two combine rather than conflict, so a route need not
change its middleware list and its handler signature together.
"""
import logging

from ..asgi import ASGIEvent
from ..websocket import (handshake_closed, mark_handshake_accepted,
                         mark_handshake_closed)

logger = logging.getLogger(__name__)

_accept  = {'type': ASGIEvent.WS_ACCEPT, 'subprotocol': None}
_close   = {'type': ASGIEvent.WS_CLOSE}


async def websocket(conn, receive, send, call_next):
    msg = await receive()

    if msg.get('type') != ASGIEvent.WS_CONNECT:
        raise ValueError(
            f'Received Message ({msg}) does not request to open a websocket connection.'
        )

    await send(_accept)
    # Publish the handshake so a downstream WebSocket object does not wait for
    # a connect event that is already gone — without this it would read the
    # client's first *message* and mistake it for the handshake.
    mark_handshake_accepted(conn)

    await call_next(conn, receive, send)

    # The handler may have closed the connection itself (``await ws.close()``,
    # or a raw handler sending the event).  A second close frame after that is
    # redundant at best, so only close what is still open.
    if not handshake_closed(conn):
        await send(_close)
        mark_handshake_closed(conn)
