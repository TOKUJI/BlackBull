import logging

from ..asgi import ASGIEvent

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
    await call_next(conn, receive, send)
    await send(_close)
