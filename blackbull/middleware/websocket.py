"""Handshake middleware — accepts the connection before the handler runs.

Predates the high-level :class:`~blackbull.websocket.WebSocket` object and
exists to strip the accept/close boilerplate from raw-triplet handlers.  It
works with either handler form: it records the handshake on the connection,
so a ``WebSocket`` object built downstream adopts that state instead of
waiting for a ``websocket.connect`` that has already been consumed.

With the object form the middleware is largely redundant — ``await
ws.accept()`` is the line it was removing — but combining them is supported
rather than refused, because a route's middleware list and its handler
signature are usually changed by different people at different times.
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
