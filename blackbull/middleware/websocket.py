from logging import getLogger

logger = getLogger(__name__)

_connect = {'type': 'websocket.connect'}
_accept  = {'type': 'websocket.accept', 'subprotocol': None}
_close   = {'type': 'websocket.close'}


async def websocket(scope, receive, send, call_next):
    msg = await receive()

    if msg.get('type') != 'websocket.connect':
        raise ValueError(
            f'Received Message ({msg}) does not request to open a websocket connection.'
        )

    await send(_accept)
    await call_next(scope, receive, send)
    await send(_close)
