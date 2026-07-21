"""Golden-path tests for ``blackbull.testing.TestClient``.

The tests below double as the worked examples for the feature: they
should be the first place a reader looks to learn how to use
TestClient against a BlackBull app.
"""

from http import HTTPMethod

import pytest

from blackbull import BlackBull
from blackbull.router import Scheme
from blackbull.testing import TestClient, WebSocketDisconnect, WebSocketTestSession


def _make_app() -> BlackBull:
    """Construct a tiny app exercising the handler shapes TestClient cares about."""
    app = BlackBull()

    @app.route(path='/')
    async def index():
        return 'hello, world'

    @app.route(path='/items/{item_id:int}')
    async def get_item(item_id: int):
        return {'id': item_id, 'kind': 'widget'}

    @app.route(path='/echo', methods=[HTTPMethod.POST])
    async def echo(body: bytes):
        return body

    @app.route(path='/echo-json', methods=[HTTPMethod.POST])
    async def echo_json(scope, receive, send):
        # Stream-aware variant — used to exercise httpx → ASGITransport
        # full-body buffering with explicit start/body events.
        chunks: list[bytes] = []
        more_body = True
        while more_body:
            event = await receive()
            chunks.append(event.get('body', b''))
            more_body = event.get('more_body', False)
        body = b''.join(chunks)
        await send({
            'type': 'http.response.start',
            'status': 200,
            'headers': [(b'content-type', b'application/json')],
        })
        await send({'type': 'http.response.body', 'body': body})

    @app.route(path='/ws', scheme=Scheme.websocket)
    async def ws_echo(scope, receive, send):
        event = await receive()
        if event['type'] != 'websocket.connect':
            await send({'type': 'websocket.close', 'code': 1002})
            return
        await send({'type': 'websocket.accept'})
        while True:
            event = await receive()
            t = event.get('type', '')
            if t == 'websocket.disconnect':
                return
            if t == 'websocket.receive':
                if event.get('text') is not None:
                    await send({'type': 'websocket.send', 'text': event['text']})
                elif event.get('bytes') is not None:
                    await send({'type': 'websocket.send', 'bytes': event['bytes']})

    @app.route(path='/ws-reject', scheme=Scheme.websocket)
    async def ws_reject(scope, receive, send):
        await receive()  # consume the websocket.connect
        await send({'type': 'websocket.close', 'code': 4401})

    @app.route(path='/ws-stream', scheme=Scheme.websocket)
    async def ws_stream(scope, receive, send):
        await receive()  # consume the websocket.connect
        await send({'type': 'websocket.accept'})
        for i in range(3):
            await send({'type': 'websocket.send', 'text': f'msg-{i}'})
        await send({'type': 'websocket.close', 'code': 1000})

    @app.route(path='/ws-stream-bytes', scheme=Scheme.websocket)
    async def ws_stream_bytes(scope, receive, send):
        await receive()
        await send({'type': 'websocket.accept'})
        for i in range(3):
            await send({'type': 'websocket.send', 'bytes': bytes([i, i, i])})
        await send({'type': 'websocket.close', 'code': 1000})

    return app


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

def test_get_returns_text() -> None:
    app = _make_app()
    with TestClient(app) as client:
        response = client.get('/')
    assert response.status_code == 200
    assert response.text == 'hello, world'


def test_path_param_coerced_and_json_response() -> None:
    app = _make_app()
    with TestClient(app) as client:
        response = client.get('/items/42')
    assert response.status_code == 200
    assert response.json() == {'id': 42, 'kind': 'widget'}


def test_post_body_round_trip_via_simplified_handler() -> None:
    app = _make_app()
    payload = b'\x00\x01\x02the quick brown fox\xff'
    with TestClient(app) as client:
        response = client.post('/echo', content=payload)
    assert response.status_code == 200
    assert response.content == payload


def test_request_object_end_to_end() -> None:
    """A handler taking the opt-in Request sees method/headers/cookies/body."""
    from blackbull import Connection as Request  # Sprint 79 alias

    app = BlackBull()

    @app.route(path='/inspect', methods=[HTTPMethod.POST])
    async def inspect(request: Request):
        return {
            'method': request.method,
            'path': request.path,
            'content_type': request.headers.get(b'content-type').decode(),
            'session': request.cookies.get('session', ''),
            'body': (await request.text()),
        }

    with TestClient(app) as client:
        response = client.post(
            '/inspect', content=b'ping',
            headers=[('Content-Type', 'text/plain')],
            cookies={'session': 'abc123'},
        )
    assert response.status_code == 200
    assert response.json() == {
        'method': 'POST',
        'path': '/inspect',
        'content_type': 'text/plain',
        'session': 'abc123',
        'body': 'ping',
    }


def test_post_streaming_body_via_full_form_handler() -> None:
    app = _make_app()
    payload = {'name': 'BlackBull', 'count': 3}
    with TestClient(app) as client:
        response = client.post('/echo-json', json=payload)
    assert response.status_code == 200
    assert response.headers['content-type'] == 'application/json'
    assert response.json() == payload


def test_404_for_unknown_path() -> None:
    app = _make_app()
    with TestClient(app) as client:
        response = client.get('/nope')
    assert response.status_code == 404


# ---------------------------------------------------------------------------
# WebSocket
# ---------------------------------------------------------------------------

def test_websocket_text_round_trip() -> None:
    app = _make_app()
    with TestClient(app) as client:
        with client.websocket_connect('/ws') as ws:
            ws.send_text('hello')
            assert ws.receive_text() == 'hello'


def test_ws_testclient_scope_matches_server_decoding() -> None:
    """Sprint 68 — the WS test client must build the same scope the real
    server would: percent-decoded ``path``, undecoded UTF-8 ``raw_path``,
    query excluded from both."""
    session = WebSocketTestSession(app=None, path='/chat/caf%C3%A9?x=%41')
    assert session.scope['path'] == '/chat/café'
    assert session.scope['raw_path'] == b'/chat/caf%C3%A9'
    assert session.scope['query_string'] == b'x=%41'


def test_ws_testclient_plain_path_unchanged() -> None:
    session = WebSocketTestSession(app=None, path='/chat/room')
    assert session.scope['path'] == '/chat/room'
    assert session.scope['raw_path'] == b'/chat/room'


def test_ws_testclient_non_ascii_query_string_utf8() -> None:
    """query_string is UTF-8 encoded, for parity with raw_path and the real
    server — a non-ASCII query must not raise (was latin-1, Sprint 68 follow-up)."""
    session = WebSocketTestSession(app=None, path='/search?q=café')
    assert session.scope['query_string'] == 'q=café'.encode('utf-8')


def test_websocket_binary_round_trip() -> None:
    app = _make_app()
    payload = b'\x00\xff\x10\x20'
    with TestClient(app) as client:
        with client.websocket_connect('/ws') as ws:
            ws.send_bytes(payload)
            assert ws.receive_bytes() == payload


def test_websocket_json_helper() -> None:
    app = _make_app()
    with TestClient(app) as client:
        with client.websocket_connect('/ws') as ws:
            ws.send_json({'msg': 'ping', 'n': 1})
            assert ws.receive_json() == {'msg': 'ping', 'n': 1}


def test_websocket_iter_text_streams_until_close() -> None:
    app = _make_app()
    with TestClient(app) as client:
        with client.websocket_connect('/ws-stream') as ws:
            messages = list(ws.iter_text())
    assert messages == ['msg-0', 'msg-1', 'msg-2']


def test_websocket_iter_bytes_streams_until_close() -> None:
    app = _make_app()
    with TestClient(app) as client:
        with client.websocket_connect('/ws-stream-bytes') as ws:
            chunks = list(ws.iter_bytes())
    assert chunks == [b'\x00\x00\x00', b'\x01\x01\x01', b'\x02\x02\x02']


def test_websocket_rejected_raises_disconnect() -> None:
    app = _make_app()
    with TestClient(app) as client:
        with pytest.raises(WebSocketDisconnect) as excinfo:
            with client.websocket_connect('/ws-reject'):
                pass
        assert excinfo.value.code == 4401


# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------

def test_lifespan_startup_and_shutdown_fire_once_each() -> None:
    app = BlackBull()
    events: list[str] = []

    @app.on_startup
    async def _startup() -> None:
        events.append('startup')

    @app.on_shutdown
    async def _shutdown() -> None:
        events.append('shutdown')

    @app.route(path='/')
    async def index():
        return 'ok'

    with TestClient(app) as client:
        assert events == ['startup']
        response = client.get('/')
        assert response.status_code == 200
    assert events == ['startup', 'shutdown']


def test_app_without_lifespan_still_works() -> None:
    """An ASGI app that doesn't speak the lifespan protocol must not break TestClient."""
    async def app(scope, receive, send):
        if scope['type'] == 'lifespan':
            # Refuse to participate — return without acking startup.  This
            # is the legacy ASGI-2.0 signal for "lifespan unsupported".
            return
        # http
        await send({
            'type': 'http.response.start',
            'status': 200,
            'headers': [(b'content-type', b'text/plain')],
        })
        await send({'type': 'http.response.body', 'body': b'no-lifespan'})

    with TestClient(app) as client:
        response = client.get('/')
    assert response.status_code == 200
    assert response.text == 'no-lifespan'


def test_lifespan_startup_failure_surfaces() -> None:
    app = BlackBull()

    @app.on_startup
    async def _boom() -> None:
        raise RuntimeError('intentional startup failure')

    with pytest.raises(RuntimeError, match='startup failed'):
        with TestClient(app):
            pass
