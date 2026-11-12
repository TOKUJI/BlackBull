"""The native ASGI-dict API warns from native code, never from BlackBull's own (BLA-672).

A warning is attributed to the line that used the dict API, fires once per
request for ``receive()``, and stays silent for the native forms, for
BlackBull's helpers and for the compatibility boundary.
"""
import warnings

import pytest

from blackbull import (BlackBull, BlackBullDeprecationWarning, Response,
                       WebSocketResponse, as_middleware, read_body)
from blackbull.middleware import websocket as ws_handshake
from blackbull.native import NativeWSMessage
from blackbull.testing import TestClient
from blackbull.testing.native import NativeClient
from blackbull.utils import Scheme
from blackbull.websocket import WebSocket


def _bbd(record) -> list[str]:
    return [str(w.message).rsplit('(', 1)[-1].rstrip(').')
            for w in record if issubclass(w.category, BlackBullDeprecationWarning)]


def _record(fn):
    with warnings.catch_warnings(record=True) as record:
        warnings.simplefilter('always')
        result = fn()
    return result, record


def _get(app, path='/', **kwargs):
    with NativeClient(app) as client:
        return client.get(path, **kwargs)


# --- send (BBD010) -------------------------------------------------------

def _dict_handler_app():
    app = BlackBull()

    @app.route(path='/')
    async def handler(conn, receive, send):
        await send({'type': 'http.response.start', 'status': 200, 'headers': []})
        await send({'type': 'http.response.body', 'body': b'ok'})

    return app


def test_a_handler_sending_dicts_warns_on_its_own_line():
    response, record = _record(lambda: _get(_dict_handler_app()))

    assert response.body == b'ok'
    assert set(_bbd(record)) == {'BBD010'}
    assert {w.filename for w in record if 'BBD010' in str(w.message)} == {__file__}


def _dict_middleware_app():
    app = BlackBull()

    async def gate(conn, receive, send, call_next):
        await send({'type': 'http.response.start', 'status': 403, 'headers': []})
        await send({'type': 'http.response.body', 'body': b'no'})

    app.use(gate)

    @app.route(path='/')
    async def handler(conn):
        return 'unreached'

    return app


def test_an_undecorated_middleware_sending_dicts_warns():
    response, record = _record(lambda: _get(_dict_middleware_app()))

    assert response.status == 403
    assert 'BBD010' in _bbd(record)


def test_a_dict_sent_through_the_host_boundary_warns():
    def run():
        with TestClient(_dict_middleware_app()) as client:
            return client.get('/')

    response, record = _record(run)

    assert response.status_code == 403
    assert 'BBD010' in _bbd(record)


def test_native_sends_do_not_warn():
    app = BlackBull()

    @app.route(path='/')
    async def handler(conn, receive, send):
        await send(Response(b'ok'))

    response, record = _record(lambda: _get(app))

    assert response.body == b'ok'
    assert _bbd(record) == []


def test_websocket_response_warns():
    event, record = _record(lambda: WebSocketResponse('hi'))

    assert event == {'type': 'websocket.send', 'text': 'hi'}
    assert _bbd(record) == ['BBD013']


# --- receive (BBD011) ----------------------------------------------------

def test_a_handler_reading_receive_warns_once_per_request():
    app = BlackBull()

    @app.route(path='/', methods=['POST'])
    async def handler(conn, receive, send):
        first = await receive()
        await receive()
        await send(Response(first['body']))

    def run():
        with NativeClient(app) as client:
            return client.post('/', body=b'abc')

    response, record = _record(run)

    assert response.body == b'abc'
    assert _bbd(record) == ['BBD011']
    assert record[0].filename == __file__


def test_a_middleware_reading_receive_warns():
    app = BlackBull()

    async def peek(conn, receive, send, call_next):
        await receive()
        await call_next(conn, receive, send)

    app.use(peek)

    @app.route(path='/')
    async def handler(conn):
        return 'ok'

    _, record = _record(lambda: _get(app))

    assert _bbd(record) == ['BBD011']


def test_body_helpers_do_not_warn():
    app = BlackBull()

    async def passthrough(conn, receive, send, call_next):
        await call_next(conn, receive, send)

    app.use(passthrough)

    @app.route(path='/body', methods=['POST'])
    async def body(conn, receive, send):
        await send(Response(await conn.body()))

    @app.route(path='/read', methods=['POST'])
    async def read(conn, receive, send):
        await send(Response(await read_body(receive)))

    def run():
        with NativeClient(app) as client:
            return [client.post(p, body=b'abc').body for p in ('/body', '/read')]

    bodies, record = _record(run)

    assert bodies == [b'abc', b'abc']
    assert _bbd(record) == []


def test_body_helpers_do_not_warn_through_the_host_boundary():
    app = BlackBull()

    @app.route(path='/', methods=['POST'])
    async def handler(conn, receive, send):
        await send(Response(await read_body(receive)))

    def run():
        with TestClient(app) as client:
            return client.post('/', content=b'abc')

    response, record = _record(run)

    assert response.content == b'abc'
    assert _bbd(record) == []


# --- WebSocket -----------------------------------------------------------

def _ws_echo(app, path, handler):
    def run():
        with TestClient(app) as client:
            with client.websocket_connect(path) as session:
                session.send_text('hi')
                return session.receive_text()
    return _record(run)


def test_a_raw_websocket_handler_warns_for_receive_and_send():
    app = BlackBull()

    @app.route(path='/ws', scheme=Scheme.websocket)
    async def raw(conn, receive, send):
        await receive()
        await send({'type': 'websocket.accept'})
        event = await receive()
        await send({'type': 'websocket.send', 'text': event['text']})

    reply, record = _ws_echo(app, '/ws', raw)

    assert reply == 'hi'
    assert set(_bbd(record)) == {'BBD010', 'BBD011'}


def test_the_websocket_object_and_native_messages_do_not_warn():
    app = BlackBull()
    app.use(ws_handshake)

    @app.route(path='/ws', scheme=Scheme.websocket)
    async def obj(ws: WebSocket):
        await ws.accept()
        await ws.send_text(await ws.receive_text())

    reply, record = _ws_echo(app, '/ws', obj)

    assert reply == 'hi'
    assert _bbd(record) == []


def test_native_ws_messages_from_a_raw_handler_do_not_warn_on_send():
    app = BlackBull()

    @app.route(path='/ws', scheme=Scheme.websocket)
    async def raw(conn, receive, send):
        ws = WebSocket(conn, receive, send)
        await ws.accept()
        await send(NativeWSMessage.text_message(await ws.receive_text()))

    reply, record = _ws_echo(app, '/ws', raw)

    assert reply == 'hi'
    assert _bbd(record) == []


# --- scope-declared middleware (BBD012) -----------------------------------

def test_scope_declared_middleware_warns_at_decoration():
    def declare():
        @as_middleware
        async def legacy(scope, receive, send, call_next):
            await call_next(scope, receive, send)
        return legacy

    _, record = _record(declare)

    assert _bbd(record) == ['BBD012']
    assert record[0].filename == __file__


def test_native_decorated_middleware_does_not_warn():
    def declare():
        @as_middleware
        async def modern(conn, receive, send, call_next):
            await call_next(conn, receive, send)
        return modern

    _, record = _record(declare)

    assert _bbd(record) == []


@pytest.mark.parametrize('client', ['native', 'host'])
def test_scope_declared_middleware_warns_only_at_decoration(client):
    with warnings.catch_warnings():
        warnings.simplefilter('ignore')

        @as_middleware
        async def legacy(scope, receive, send, call_next):
            await call_next(scope, receive, send)

    app = BlackBull()
    app.use(legacy)

    @app.route(path='/')
    async def handler(conn):
        return 'ok'

    def run():
        if client == 'native':
            return _get(app).body
        with TestClient(app) as c:
            return c.get('/').content

    body, record = _record(run)

    assert body == b'ok'
    assert _bbd(record) == []


def test_a_websocket_dict_through_middleware_warns_once_on_the_handler_line():
    from blackbull.middleware import Compression

    app = BlackBull()
    app.use(Compression())

    @app.route(path='/ws', scheme=Scheme.websocket)
    async def raw(conn, receive, send):
        ws = WebSocket(conn, receive, send)
        await ws.accept()
        await send({'type': 'websocket.send', 'text': await ws.receive_text()})

    reply, record = _ws_echo(app, '/ws', raw)

    assert reply == 'hi'
    sent = [w for w in record if 'BBD010' in str(w.message)]
    assert [w.filename for w in sent] == [__file__]
