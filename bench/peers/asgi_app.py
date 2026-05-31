"""Shared minimal ASGI 3.0 application for peer-server comparison.

Loaded by uvicorn, hypercorn, granian, and daphne. Identical handler code
across all four servers — only the runner differs. Routes mirror
``bench/app.py`` (BlackBull) at the wire level: same paths, same response
bodies, same content-types, same status codes.

Run with one of::

    uvicorn   bench.peers.asgi_app:app --port 8443 --ssl-certfile tests/cert.pem --ssl-keyfile tests/key.pem
    hypercorn bench.peers.asgi_app:app --bind 0.0.0.0:8443 --certfile tests/cert.pem --keyfile tests/key.pem
    granian   --interface asgi bench.peers.asgi_app:app --port 8443 --ssl-certificate tests/cert.pem --ssl-keyfile tests/key.pem
    daphne    -e ssl:8443:privateKey=tests/key.pem:certKey=tests/cert.pem bench.peers.asgi_app:app

Note daphne and uvicorn do not negotiate HTTP/2 — they're HTTP/1.1 + WS only.
"""
import os
from http import HTTPStatus
# -- pre-encoded bodies (allocated once at import) --------------------------

_PLAINTEXT = b'Hello, World!'
_JSON      = b'{"message":"Hello, World!"}'
_PONG      = b'pong'
_1KB       = os.urandom(1024)
_16KB      = os.urandom(16000)
_64KB      = os.urandom(65536)
_1MB       = os.urandom(1024 * 1024)

# -- header tuples (allocated once) -----------------------------------------

_H_PLAIN  = [(b'content-type', b'text/plain; charset=utf-8')]
_H_JSON   = [(b'content-type', b'application/json')]
_H_HTML   = [(b'content-type', b'text/html; charset=utf-8')]
_H_OCTET  = [(b'content-type', b'application/octet-stream')]

# Map (method, path) -> (status, headers, body) for static routes.
_STATIC = {
    ('GET', '/ping'):      (200, _H_HTML,  _PONG),
    ('GET', '/plaintext'): (200, _H_PLAIN, _PLAINTEXT),
    ('GET', '/json'):      (200, _H_JSON,  _JSON),
    ('GET', '/1kb'):       (200, _H_HTML,  _1KB),
    ('GET', '/16kb'):      (200, _H_HTML,  _16KB),
    ('GET', '/64kb'):      (200, _H_HTML,  _64KB),
    ('GET', '/1mb'):       (200, _H_HTML,  _1MB),
}


async def _send_static(send, status, headers, body):
    await send({'type': 'http.response.start',
                'status': status,
                'headers': headers})
    await send({'type': 'http.response.body',
                'body': body})


async def _echo(scope, receive, send):
    chunks = []
    while True:
        msg = await receive()
        if msg['type'] != 'http.request':
            break
        body = msg.get('body', b'')
        if body:
            chunks.append(body)
        if not msg.get('more_body', False):
            break
    payload = b''.join(chunks)
    await send({'type': 'http.response.start',
                'status': HTTPStatus.OK,
                'headers': _H_OCTET})
    await send({'type': 'http.response.body',
                'body': payload})


async def _ws_echo(scope, receive, send):
    msg = await receive()
    if msg.get('type') != 'websocket.connect':
        return
    await send({'type': 'websocket.accept'})
    while True:
        msg = await receive()
        t = msg.get('type', '')
        if t == 'websocket.disconnect':
            break
        if t != 'websocket.receive':
            continue
        text = msg.get('text')
        if text is not None:
            await send({'type': 'websocket.send', 'text': text})
        else:
            await send({'type': 'websocket.send',
                        'bytes': msg.get('bytes') or b''})


async def _not_found(send):
    await send({'type': 'http.response.start', 'status': HTTPStatus.NOT_FOUND,
                'headers': _H_PLAIN})
    await send({'type': 'http.response.body', 'body': b'not found'})


async def app(scope, receive, send):
    t = scope['type']
    if t == 'http':
        method = scope['method']
        path = scope['path']
        hit = _STATIC.get((method, path))
        if hit is not None:
            await _send_static(send, *hit)
            return
        if method == 'POST' and path == '/echo':
            await _echo(scope, receive, send)
            return
        await _not_found(send)
        return
    if t == 'websocket':
        if scope.get('path') == '/ws':
            await _ws_echo(scope, receive, send)
        else:
            await send({'type': 'websocket.close'})
        return
    if t == 'lifespan':
        while True:
            msg = await receive()
            if msg['type'] == 'lifespan.startup':
                await send({'type': 'lifespan.startup.complete'})
            elif msg['type'] == 'lifespan.shutdown':
                await send({'type': 'lifespan.shutdown.complete'})
                return
