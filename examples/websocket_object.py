"""The high-level ``WebSocket`` object for WebSocket handlers.

Declare a parameter annotated ``WebSocket`` (any name) — or a bare parameter
named ``ws``/``websocket`` — and the router hands the handler the connection
as an object instead of raw ASGI event dicts.

The raw ``(conn, receive, send)`` form still works and is not deprecated;
``/raw-echo`` below is the same echo server written that way, for comparison.

Try it:

    python examples/websocket_object.py

    # any WebSocket client, e.g.:
    python -c "
    import asyncio, websockets
    async def main():
        async with websockets.connect('ws://localhost:8000/echo') as ws:
            await ws.send('hello'); print(await ws.recv())
    asyncio.run(main())
    "
"""
from blackbull import BlackBull, WebSocket, WebSocketDisconnect
from blackbull.utils import Scheme

app = BlackBull()


@app.route(path='/echo', scheme=Scheme.websocket)
async def echo(ws: WebSocket):
    """The whole echo server.  The loop ends when the client goes away."""
    await ws.accept()
    async for message in ws:
        await ws.send(message)


@app.route(path='/json', scheme=Scheme.websocket)
async def json_stream(ws: WebSocket):
    """Structured messages, and the close code when it matters."""
    await ws.accept()
    count = 0
    try:
        while True:
            payload = await ws.receive_json()
            count += 1
            await ws.send_json({'seen': count, 'echo': payload})
    except WebSocketDisconnect as exc:
        print(f'client left after {count} messages: {exc.code} {exc.reason}')


@app.route(path='/rooms/{room}', scheme=Scheme.websocket)
async def room(ws: WebSocket, room: str, since: int = 0):
    """Path and query params inject straight into the signature.

    ``room`` matches the ``{room}`` placeholder; ``since`` has no placeholder,
    so it comes from the query string (``/rooms/lobby?since=7``) with 0 as its
    default.  A query param must carry its annotation — that is what tells the
    router it is one.
    """
    await ws.accept()
    await ws.send_text(f'welcome to {room} (from {since})')
    async for message in ws:
        await ws.send_text(f'[{room}] {message}')


@app.route(path='/private', scheme=Scheme.websocket)
async def private(ws: WebSocket):
    """close() before accept() rejects the handshake outright."""
    token = ws.headers.get(b'authorization', b'')
    if token != b'Bearer letmein':
        await ws.close(4401, 'unauthorized')
        return
    await ws.accept()
    await ws.send_text('welcome')
    await ws.close()


@app.route(path='/raw-echo', scheme=Scheme.websocket)
async def raw_echo(conn, receive, send):
    """The same echo server in the raw event form — still fully supported."""
    await receive()                                   # websocket.connect
    await send({'type': 'websocket.accept'})
    while True:
        event = await receive()
        if event['type'] == 'websocket.disconnect':
            break
        if event['type'] == 'websocket.receive':
            if event.get('text') is not None:
                await send({'type': 'websocket.send', 'text': event['text']})
            else:
                await send({'type': 'websocket.send', 'bytes': event['bytes']})


if __name__ == '__main__':
    app.run(port=8000)
