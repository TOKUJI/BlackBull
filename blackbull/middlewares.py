websocket_connect = {'type': 'websocket.connect'}
websocket_accept = {"type": "websocket.accept", "subprotocol": None}


async def websocket(scope, receive, send, inner):
    msg = await receive()

    if msg != websocket_connect:
        raise ValueError('Received Message does not request to open a websocket connection.')

    await send(websocket_accept)
    await inner(scope, receive, send)
    await send({'type': 'websocket.close'})