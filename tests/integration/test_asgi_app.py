"""A foreign ASGI 3.0 application on BlackBull's server, through ASGIApp (BLA-673)."""
import asyncio
import logging

import pytest

from blackbull import ASGIApp, BlackBull, Response
from blackbull.client import HTTP1Client, HTTP2Client, WebSocketClient
from blackbull.env import reset_settings_cache
from blackbull.router import ConfigurationError
from blackbull.server import Server
from blackbull.utils import Scheme
from blackbull.websocket import WebSocket
from tests.harness import served


pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

CLIENTS = pytest.mark.parametrize('client_type', [HTTP1Client, HTTP2Client],
                                  ids=['h1', 'h2'])


@pytest.fixture
def force_env(monkeypatch):
    def set_force(value: str):
        monkeypatch.setenv('BB_FORCE_ASGI_SCOPE', value)
        reset_settings_cache()
    yield set_force
    monkeypatch.delenv('BB_FORCE_ASGI_SCOPE', raising=False)
    reset_settings_cache()


async def _ok(scope, receive, send):
    if scope['type'] == 'lifespan':
        return
    await send({'type': 'http.response.start', 'status': 200,
                'headers': [(b'content-type', b'text/plain')]})
    await send({'type': 'http.response.body', 'body': b'ok'})


async def _get(app, path='/', client_type=HTTP1Client, **server_kwargs):
    async with served(app, **server_kwargs) as server:
        async with client_type('127.0.0.1', server.server.port) as client:
            return await asyncio.wait_for(client.request('GET', path), 5)


# --- selecting the adapter ----------------------------------------------

@CLIENTS
async def test_asgi_true_serves_a_foreign_app_an_asgi_scope(client_type):
    seen = []

    async def foreign(scope, receive, send):
        seen.append(scope)
        await _ok(scope, receive, send)

    reply = await _get(foreign, '/p?q=1', client_type, asgi=True)

    assert (reply.status, reply.body) == (200, b'ok')
    scope = seen[0]
    assert type(scope) is dict
    assert (scope['type'], scope['method'], scope['path'], scope['query_string']) == (
        'http', 'GET', '/p', b'q=1')
    assert all(type(k) is bytes and type(v) is bytes for k, v in scope['headers'])


async def test_the_env_var_selects_the_adapter(force_env):
    force_env('1')

    reply = await _get(_ok)

    assert (reply.status, reply.body) == (200, b'ok')


async def test_a_bare_native_callable_is_not_wrapped(force_env):
    force_env('0')
    seen = []

    async def native(conn, receive, send):
        seen.append(type(conn).__name__)
        await send(Response(b'native').to_native())

    reply = await _get(native)

    assert (reply.body, seen) == (b'native', ['Connection'])


def test_asgi_false_with_the_env_var_is_refused(force_env):
    force_env('1')

    with pytest.raises(ConfigurationError, match='BB_FORCE_ASGI_SCOPE'):
        Server(_ok, asgi=False)


def test_unknown_server_keywords_are_rejected():
    with pytest.raises(TypeError):
        Server(_ok, no_such_option=1)


# --- supervising the foreign app -----------------------------------------

def _misbehaving(record):
    async def foreign(scope, receive, send):
        if scope['type'] == 'lifespan':
            return
        path = scope['path']
        if path == '/raise':
            raise ValueError('boom')
        if path == '/return':
            return
        try:
            if path == '/body-first':
                await send({'type': 'http.response.body', 'body': b'x'})
            elif path == '/bogus':
                await send({'type': 'http.bogus'})
            elif path == '/twice':
                await send({'type': 'http.response.start', 'status': 200, 'headers': []})
                await send({'type': 'http.response.start', 'status': 200, 'headers': []})
            elif path == '/after-end':
                await _ok(scope, receive, send)
                await send({'type': 'http.response.body', 'body': b'late'})
        except RuntimeError:
            record.append(path)
            if path == '/twice':
                await send({'type': 'http.response.body', 'body': b'once'})
            return
    return foreign


@CLIENTS
@pytest.mark.parametrize('path', ['/raise', '/return'])
async def test_failing_before_the_response_starts_answers_500_and_logs(
        client_type, path, caplog):
    with caplog.at_level(logging.ERROR, logger='blackbull'):
        reply = await _get(_misbehaving([]), path, client_type, asgi=True)

    assert reply.status == 500
    assert any(r.levelno == logging.ERROR for r in caplog.records)


@CLIENTS
@pytest.mark.parametrize('path, status, body', [
    ('/body-first', 500, None),
    ('/bogus', 500, None),
    ('/twice', 200, b'once'),
    ('/after-end', 200, b'ok'),
])
async def test_send_misuse_raises_runtime_error_in_the_app(client_type, path, status, body):
    record = []

    reply = await _get(_misbehaving(record), path, client_type, asgi=True)

    assert record == [path]
    assert reply.status == status
    if body is not None:
        assert reply.body == body


@CLIENTS
async def test_returning_mid_response_ends_the_exchange_without_a_hang(client_type):
    async def foreign(scope, receive, send):
        await send({'type': 'http.response.start', 'status': 200, 'headers': []})
        await send({'type': 'http.response.body', 'body': b'part', 'more_body': True})

    with pytest.raises(Exception) as caught:
        await _get(foreign, '/', client_type, asgi=True)

    assert not isinstance(caught.value, asyncio.TimeoutError)


async def test_send_after_disconnect_raises_an_oserror_and_closes_quietly(caplog):
    done = asyncio.Event()
    raised = []

    async def foreign(scope, receive, send):
        try:
            while (await receive())['type'] != 'http.disconnect':
                pass
            try:
                await send({'type': 'http.response.start', 'status': 200, 'headers': []})
            except OSError as exc:
                raised.append(exc)
                raise
        finally:
            done.set()

    with caplog.at_level(logging.ERROR, logger='blackbull'):
        async with served(foreign, asgi=True) as server:
            wire = await server.wire()
            await wire.send(b'POST / HTTP/1.1\r\nhost: x\r\ncontent-length: 10\r\n\r\nab')
            await asyncio.sleep(0.05)
            await wire.aclose()
            await asyncio.wait_for(done.wait(), 5)

    assert len(raised) == 1
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]


@CLIENTS
async def test_a_disconnect_listener_does_not_cut_a_streaming_response(client_type):
    """A receive() after the body waits for the client to go (Starlette's listener)."""
    heard = []

    async def foreign(scope, receive, send):
        while (await receive()).get('more_body'):
            pass

        async def listen():
            heard.append((await receive())['type'])

        listener = asyncio.ensure_future(listen())
        await asyncio.sleep(0)
        await send({'type': 'http.response.start', 'status': 200, 'headers': []})
        for part in (b'a', b'b'):
            await send({'type': 'http.response.body', 'body': part, 'more_body': True})
            await asyncio.sleep(0.01)
        await send({'type': 'http.response.body', 'body': b'c'})
        await asyncio.wait_for(listener, 5)

    reply = await _get(foreign, '/', client_type, asgi=True)

    assert reply.body == b'abc'
    assert heard == ['http.disconnect']


# --- events ----------------------------------------------------------------

def _listen(app, names):
    fired = {name: [] for name in names}
    for name in names:
        @app.on(name, blocking=True)
        async def record(event, name=name):
            fired[name].append(event.detail)
    return fired


@CLIENTS
async def test_the_adapter_fires_each_request_event_once(client_type):
    app = ASGIApp(_ok)
    fired = _listen(app, ['request_received', 'before_handler', 'after_handler',
                          'request_completed', 'scope_completed'])

    reply = await _get(app, '/', client_type)

    assert reply.status == 200
    assert {name: len(details) for name, details in fired.items()} == dict.fromkeys(fired, 1)
    assert fired['request_completed'][0]['status'] == 200
    assert fired['scope_completed'][0]['exception'] is None


async def test_a_raising_app_reaches_the_terminal_events():
    app = ASGIApp(_misbehaving([]))
    fired = _listen(app, ['after_handler', 'scope_completed'])

    reply = await _get(app, '/raise')

    assert reply.status == 500
    assert isinstance(fired['after_handler'][0]['exception'], ValueError)
    assert isinstance(fired['scope_completed'][0]['exception'], ValueError)


async def test_a_request_received_interceptor_gates_the_app():
    called = []

    async def foreign(scope, receive, send):
        if scope['type'] == 'lifespan':
            return
        called.append(scope['path'])
        await _ok(scope, receive, send)

    app = ASGIApp(foreign)

    @app.intercept('request_received')
    async def deny(event):
        raise PermissionError('no')

    reply = await _get(app)

    assert reply.status == 500
    assert called == []


def _blackbull_app():
    bb = BlackBull()

    @bb.route(path='/')
    async def index(conn):
        return 'bb'

    @bb.route(path='/ws', scheme=Scheme.websocket)
    async def echo(ws: WebSocket):
        await ws.accept()
        await ws.send_text(await ws.receive_text())

    return bb


@pytest.mark.parametrize('form', ['direct', 'wrapped'])
async def test_around_a_blackbull_app_the_inner_events_fire_once(form):
    bb = _blackbull_app()
    fired = _listen(bb, ['request_received', 'request_completed'])
    if form == 'direct':
        app = bb
    else:
        async def app(scope, receive, send):
            await bb(scope, receive, send)
        app.__wrapped__ = bb

    adapter = ASGIApp(app)
    on_adapter = _listen(adapter, ['request_completed'])

    reply = await _get(adapter)

    assert reply.body == b'bb'
    assert [len(fired[n]) for n in fired] == [1, 1]
    assert len(on_adapter['request_completed']) == 1


# --- WebSocket and lifespan (interim) ---------------------------------------

async def test_a_foreign_websocket_request_is_closed_cleanly():
    called = []

    async def foreign(scope, receive, send):
        called.append(scope['type'])

    async with served(foreign, asgi=True) as server:
        async with WebSocketClient('127.0.0.1', server.server.port) as client:
            with pytest.raises(Exception):
                await asyncio.wait_for(client.connect('/ws'), 5)

    assert called == []


async def test_a_wrapped_blackbull_app_keeps_its_websockets():
    bb = _blackbull_app()
    bb.available_ws_protocols = ['chat']
    async with served(bb, asgi=True) as server:
        async with WebSocketClient('127.0.0.1', server.server.port) as client:
            ws = await client.connect('/ws', subprotocols=[b'chat'])
            assert ws.subprotocol == b'chat'
            await ws.send_text('hi')
            reply = (await ws.receive())['text']
            await ws.close()

    assert reply == 'hi'


async def test_the_foreign_lifespan_is_forwarded():
    phases = []

    async def foreign(scope, receive, send):
        if scope['type'] == 'lifespan':
            while True:
                message = await receive()
                phases.append(message['type'])
                await send({'type': message['type'] + '.complete'})
                if message['type'] == 'lifespan.shutdown':
                    return
        await _ok(scope, receive, send)

    server = Server(foreign, asgi=True)
    await server.startup()
    await server.shutdown()

    assert phases == ['lifespan.startup', 'lifespan.shutdown']
