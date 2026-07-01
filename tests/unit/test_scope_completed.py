"""Tests for the ``scope_completed`` event — the guaranteed, cross-protocol
terminal event emitted once per ASGI scope from ``BlackBull.__call__``.

Unlike ``request_completed`` (HTTP-only, server-level) this fires for HTTP,
WebSocket, and gRPC, on success and on error, under any server — here driven
through TestClient (HTTP/WS) and a direct ``app(...)`` call (gRPC).  It is the
home of the awaited-isolated ``@app.on(..., blocking=True)`` cleanup hook.
"""

import pytest

from blackbull import BlackBull
from blackbull.router import Scheme
from blackbull.testing import TestClient


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

def test_fires_once_for_successful_request() -> None:
    app = BlackBull()
    seen = []

    @app.on('scope_completed', blocking=True)
    async def cleanup(event):
        seen.append(event.detail)

    @app.route(path='/ok')
    async def ok():
        return 'hi'

    with TestClient(app) as client:
        r = client.get('/ok')
    assert r.status_code == 200
    assert len(seen) == 1
    assert seen[0]['type'] == 'http'
    assert seen[0]['path'] == '/ok'
    assert seen[0]['exception'] is None


def test_fires_with_exception_on_handler_error() -> None:
    app = BlackBull()
    seen = []

    @app.on('scope_completed', blocking=True)
    async def cleanup(event):
        seen.append(event.detail['exception'])

    @app.route(path='/boom')
    async def boom():
        raise ValueError('handler failed')

    with TestClient(app) as client:
        r = client.get('/boom')
    # The framework turned the error into a 500, and the cleanup hook can see it.
    assert r.status_code == 500
    assert len(seen) == 1
    assert isinstance(seen[0], ValueError)


def test_fires_for_404_without_exception() -> None:
    app = BlackBull()
    seen = []

    @app.on('scope_completed', blocking=True)
    async def cleanup(event):
        seen.append(event.detail)

    with TestClient(app) as client:
        r = client.get('/nope')
    assert r.status_code == 404
    assert len(seen) == 1
    assert seen[0]['path'] == '/nope'
    assert seen[0]['exception'] is None  # a 404 is not an exception


def test_fires_exactly_once_per_request_in_sequence() -> None:
    app = BlackBull()
    count = []

    @app.on('scope_completed', blocking=True)
    async def cleanup(event):
        count.append(event.detail['path'])

    @app.route(path='/r')
    async def r():
        return 'r'

    with TestClient(app) as client:
        for _ in range(3):
            client.get('/r')
    assert count == ['/r', '/r', '/r']


# ---------------------------------------------------------------------------
# Blocking-cleanup semantics through the real dispatch path
# ---------------------------------------------------------------------------

def test_blocking_cleanup_completes_and_isolates() -> None:
    app = BlackBull()
    order = []

    @app.on('scope_completed', blocking=True)
    async def first(event):
        raise RuntimeError('cleanup boom')  # isolated

    @app.on('scope_completed', blocking=True)
    async def second(event):
        order.append('second')

    @app.route(path='/ok')
    async def ok():
        return 'ok'

    with TestClient(app) as client:
        r = client.get('/ok')
    # Response unaffected by a failing cleanup hook; siblings still run.
    assert r.status_code == 200
    assert r.text == 'ok'
    assert order == ['second']


# ---------------------------------------------------------------------------
# WebSocket
# ---------------------------------------------------------------------------

def test_fires_for_websocket_connection() -> None:
    app = BlackBull()
    seen = []

    @app.on('scope_completed', blocking=True)
    async def cleanup(event):
        seen.append((event.detail['type'], event.detail['path']))

    @app.route(path='/ws', scheme=Scheme.websocket)
    async def ws(scope, receive, send):
        await receive()
        await send({'type': 'websocket.accept'})
        while True:
            ev = await receive()
            if ev['type'] == 'websocket.disconnect':
                return
            if ev['type'] == 'websocket.receive':
                await send({'type': 'websocket.send', 'text': ev['text']})

    with TestClient(app) as client:
        with client.websocket_connect('/ws') as wsconn:
            wsconn.send_text('ping')
            assert wsconn.receive_text() == 'ping'
    assert seen == [('websocket', '/ws')]


# ---------------------------------------------------------------------------
# gRPC (application/grpc rides the HTTP scope through __call__ → _dispatch)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_fires_for_grpc_call() -> None:
    from blackbull.grpc import GrpcServiceRegistry
    from blackbull.grpc.asgi import encode_message

    reg = GrpcServiceRegistry()

    @reg.method('/echo.Echo/Echo')
    async def echo(request, context):
        return request[::-1]

    app = BlackBull()
    app.enable_grpc(reg)
    seen = []

    @app.on('scope_completed', blocking=True)
    async def cleanup(event):
        seen.append((event.detail['type'], event.detail['path']))

    scope = {
        'type': 'http', 'path': '/echo.Echo/Echo', 'method': 'POST',
        'headers': [(b'content-type', b'application/grpc'), (b':method', b'POST')],
        'client': ('127.0.0.1', 9),
    }
    sent = [False]

    async def receive():
        if not sent[0]:
            sent[0] = True
            return {'type': 'http.request', 'body': encode_message(b'abc'),
                    'more_body': False}
        return {'type': 'http.request', 'body': b'', 'more_body': False}

    async def send(event):
        pass

    await app(scope, receive, send)
    assert seen == [('http', '/echo.Echo/Echo')]


# ---------------------------------------------------------------------------
# Fast path: no listener → no overhead, app still works
# ---------------------------------------------------------------------------

def test_no_listener_is_a_noop() -> None:
    app = BlackBull()

    @app.route(path='/ok')
    async def ok():
        return 'ok'

    assert app._dispatcher.has_listeners('scope_completed') is False
    with TestClient(app) as client:
        r = client.get('/ok')
    assert r.status_code == 200
    assert r.text == 'ok'
