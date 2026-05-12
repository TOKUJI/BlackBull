import logging

from ..server.constants import ASGIEvent

logger = logging.getLogger(__name__)

_connect = {'type': ASGIEvent.WS_CONNECT}
_accept  = {'type': ASGIEvent.WS_ACCEPT, 'subprotocol': None}
_close   = {'type': ASGIEvent.WS_CLOSE}


async def websocket(scope, receive, send, call_next):
    msg = await receive()

    if msg.get('type') != ASGIEvent.WS_CONNECT:
        raise ValueError(
            f'Received Message ({msg}) does not request to open a websocket connection.'
        )

    await send(_accept)
    await call_next(scope, receive, send)
    await send(_close)
