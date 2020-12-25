from logging import getLogger

logger = getLogger(__name__)

websocket_connect = {'type': 'websocket.connect'}
websocket_accept = {'type': 'websocket.accept', 'subprotocol': None}
websocket_close = {'type': 'websocket.close'}


async def websocket(scope, receive, send, inner):
    msg = await receive()

    if msg != websocket_connect:
        raise ValueError(f'Received Message ({msg})does not request to open a websocket connection.')

    await send(websocket_accept)
    await inner(scope, receive, send)
    await send(websocket_close)
