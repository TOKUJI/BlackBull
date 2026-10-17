"""An HTTP/1.1 connection over its life: reuse, failure, logging, both dispatch lanes."""
import asyncio
import gc
import logging
import ssl

import pytest
from websockets.asyncio.client import connect

from blackbull import BlackBull
from blackbull.env import reset_settings_cache

from tests.harness import served

from ._dual_path_corpus import CORPUS, normalise

pytestmark = pytest.mark.asyncio


def _get(path=b'/', *fields):
    return b'\r\n'.join([b'GET ' + path + b' HTTP/1.1', b'Host: x', *fields]) + b'\r\n\r\n'


def _counting(calls: list, *, raise_=False):
    async def app(conn, receive, send):
        calls.append(conn.path)
        if raise_:
            raise RuntimeError('handler blew up')
        await send({'type': 'http.response.start', 'status': 200, 'headers': []})
        await send({'type': 'http.response.body', 'body': conn.path.encode()})
    return app


async def test_pipelined_requests_are_each_answered_in_order():
    calls: list = []
    async with served(_counting(calls)) as s:
        w = await s.wire()
        await w.send(_get(b'/alpha') + _get(b'/beta'))
        bodies = [(await w.response()).body, (await w.response()).body]
    assert (calls, bodies) == (['/alpha', '/beta'], [b'/alpha', b'/beta'])


async def test_connection_close_ends_the_connection_after_its_response():
    calls: list = []
    async with served(_counting(calls)) as s:
        w = await s.wire()
        await w.send(_get(b'/one', b'Connection: close') + _get(b'/two'))
        assert (await w.response()).status == 200
        assert await w.closed()
    assert calls == ['/one']


async def test_a_request_cut_short_after_a_complete_one_is_not_dispatched():
    calls: list = []
    async with served(_counting(calls)) as s:
        w = await s.wire()
        await w.send(_get(b'/one') + b'GET /two HTTP')
        w.half_close()
        assert (await w.response()).status == 200
    assert calls == ['/one']


async def test_a_raising_foreign_app_is_contained_and_ends_the_connection():
    calls: list = []
    async with served(_counting(calls, raise_=True)) as s:
        w = await s.wire()
        await w.send(_get(b'/first') + _get(b'/second'))
        await w.recv_all()
    assert calls == ['/first']


async def test_a_body_is_read_through_the_connection_buffer():
    payload = b'x' * 5000

    async def echo(conn, receive, send):
        body = b''
        while True:
            event = await receive()
            body += event.get('body', b'')
            if not event.get('more_body'):
                break
        await send({'type': 'http.response.start', 'status': 200, 'headers': []})
        await send({'type': 'http.response.body', 'body': body})

    async with served(echo) as s:
        w = await s.wire()
        await w.send(b'POST / HTTP/1.1\r\nHost: x\r\nContent-Length: 5000\r\n\r\n' + payload)
        assert (await w.response()).body == payload


async def test_a_request_timeout_answers_408_and_is_counted(monkeypatch, caplog):
    monkeypatch.setenv('BB_REQUEST_TIMEOUT', '0.05')
    reset_settings_cache()
    caplog.set_level(logging.WARNING, logger='blackbull.caps')

    async def stuck(conn, receive, send):
        await asyncio.Event().wait()

    try:
        async with served(stuck) as s:
            w = await s.wire()
            await w.send(_get())
            assert (await w.response()).status == 408
            assert await w.closed()
    finally:
        reset_settings_cache()
    assert [(r.cap, r.protocol, r.scope_path) for r in caplog.records
            if r.name == 'blackbull.caps'] == [('request_timeout', 'http1', '/')]


# -- access log -----------------------------------------------------------

@pytest.fixture
def access(caplog):
    caplog.set_level(logging.INFO, logger='blackbull.access')
    return lambda: [r.message for r in caplog.records if r.name == 'blackbull.access']


@pytest.mark.parametrize('status', [200, 404])
async def test_each_request_is_logged_with_its_status(access, status):
    async def app(conn, receive, send):
        await send({'type': 'http.response.start', 'status': status, 'headers': []})
        await send({'type': 'http.response.body', 'body': b''})

    async with served(app) as s:
        w = await s.wire()
        await w.send(_get(b'/status'))
        await w.response()
    [line] = access()
    assert 'GET /status' in line and f' {status} ' in line


@pytest.mark.parametrize('close,code', [(True, 1001), (False, 1006)])
async def test_a_websocket_session_is_logged_with_its_close_code(access, close, code):
    async def app(conn, receive, send):
        await receive()
        await send({'type': 'websocket.accept'})
        while (await receive())['type'] != 'websocket.disconnect':
            pass

    async with served(app) as s:
        ws = await connect(f'ws://127.0.0.1:{s.server.port}/chat')
        if close:
            await ws.close(code=1001)
        else:
            ws.transport.abort()
        for _ in range(100):
            if access():
                break
            await asyncio.sleep(0.01)
    [line] = access()
    assert '/chat' in line and f'101 close={code}' in line


@pytest.mark.parametrize('foreign,listener,logged,record', [
    (False, False, False, False), (False, True, False, True), (False, False, True, True),
    (True, False, False, False),
])
async def test_the_access_record_exists_only_for_a_consumer(caplog, foreign, listener, logged, record):
    """A record nobody reads is not built, on BlackBull's own apps and on foreign ones."""
    caplog.set_level(logging.INFO if logged else logging.WARNING, logger='blackbull.access')
    seen = []

    async def foreign_app(conn, receive, send):
        seen.append('access_log' in conn.state)
        await send({'type': 'http.response.start', 'status': 200, 'headers': []})
        await send({'type': 'http.response.body', 'body': b''})

    app = foreign_app if foreign else BlackBull()
    if not foreign:
        @app.route(path='/')
        async def index(conn):
            seen.append('access_log' in conn.state)
            return b'ok'

    if listener:
        @app.on('request_completed')
        async def _completed(event):
            pass

    async with served(app) as s:
        w = await s.wire()
        await w.send(_get())
        await w.response()
    assert seen == [record]


# -- connection identity --------------------------------------------------

async def test_a_websocket_session_has_a_twenty_hex_connection_id():
    ids = []

    async def app(conn, receive, send):
        await receive()
        ids.append(conn.connection_id)
        await send({'type': 'websocket.accept'})
        await receive()

    async with served(app) as s:
        async with connect(f'ws://127.0.0.1:{s.server.port}/ws'):
            pass
    assert len(ids[0]) == 20 and set(ids[0]) <= set('0123456789abcdef')


async def test_a_long_websocket_message_is_unmasked_whole():
    async def echo(conn, receive, send):
        await receive()
        await send({'type': 'websocket.accept'})
        event = await receive()
        await send({'type': 'websocket.send', 'text': event['text']})
        await receive()

    message = ''.join(chr(0x41 + i % 26) for i in range(1001))
    async with served(echo) as s:
        async with connect(f'ws://127.0.0.1:{s.server.port}/ws') as ws:
            await ws.send(message)
            assert await ws.recv() == message


@pytest.mark.parametrize('tls', [False, True])
async def test_a_websocket_scheme_follows_tls(tls):
    schemes = []

    async def app(conn, receive, send):
        schemes.append(conn.scheme)
        await receive()
        await send({'type': 'websocket.accept'})
        await receive()

    ctx = None
    if tls:
        ctx = ssl.create_default_context()
        ctx.check_hostname, ctx.verify_mode = False, ssl.CERT_NONE
    async with served(app, tls=tls) as s:
        async with connect(f'{"wss" if tls else "ws"}://127.0.0.1:{s.server.port}/ws', ssl=ctx):
            pass
    assert schemes == ['wss' if tls else 'ws']


# -- the native and forced-ASGI lanes ------------------------------------

def _corpus_app():
    a = BlackBull()

    @a.route(path='/')
    async def _root():
        return 'hello'

    @a.route(path='/echo', methods=['POST'])
    async def _echo(conn, receive, send):
        event = await receive()
        await send(event.get('body', b'') or b'(empty)', 200)

    @a.route(path='/café')
    async def _unicode_path():
        return 'cafe'

    return a


async def _answer(request: bytes) -> bytes:
    async with served(_corpus_app()) as s:
        w = await s.wire()
        await w.send(request)
        w.half_close()
        return normalise(await w.recv_all())


@pytest.mark.parametrize('name', sorted(CORPUS))
async def test_both_lanes_answer_byte_for_byte_alike(name, monkeypatch):
    request = CORPUS[name].raw
    reset_settings_cache()
    native = await _answer(request)
    monkeypatch.setenv('BB_FORCE_ASGI_SCOPE', '1')
    reset_settings_cache()
    try:
        forced = await _answer(request)
    finally:
        monkeypatch.delenv('BB_FORCE_ASGI_SCOPE')
        reset_settings_cache()
    assert native and forced == native


@pytest.mark.parametrize('lane', ['native', 'forced'])
async def test_head_reaches_the_get_handler_on_both_lanes(lane, monkeypatch):
    if lane == 'forced':
        monkeypatch.setenv('BB_FORCE_ASGI_SCOPE', '1')
    reset_settings_cache()
    try:
        raw = await _answer(b'HEAD / HTTP/1.1\r\nHost: localhost\r\n\r\n')
    finally:
        reset_settings_cache()
    head, _, body = raw.partition(b'\r\n\r\n')
    assert head.startswith(b'HTTP/1.1 200 ') and body == b''
    assert b'content-length: 5' in head.lower()


# -- per-request garbage ------------------------------------------------

async def test_a_request_leaves_no_reference_cycle():
    """The v0.60.0 tail-latency regression: each request's graph needed the cyclic GC."""
    app = BlackBull()

    @app.route(path='/ping')
    async def ping():
        return b'pong'

    @app.route(path='/echo', methods=['POST'])
    async def echo(request):
        return await request.body()

    requests = [_get(b'/ping'), b'POST /echo HTTP/1.1\r\nHost: x\r\nContent-Length: 5\r\n\r\nhello']
    async with served(app) as s:
        w = await s.wire()
        for raw in requests:
            for _ in range(20):
                await w.send(raw)
                await w.response()
            gc.collect()
            gc.disable()
            try:
                for _ in range(300):
                    await w.send(raw)
                    await w.response()
                per_request = gc.collect() / 300
            finally:
                gc.enable()
            assert per_request < 1.0, f'{raw.split()[0]!r} leaves {per_request:.1f} cycle objects'


async def test_an_external_asgi_request_leaves_no_reference_cycle():
    """uvicorn and httpx.ASGITransport enter through ``from_scope``."""
    app = BlackBull()

    @app.route(path='/ping')
    async def ping():
        return b'pong'

    scope = {'type': 'http', 'asgi': {'version': '3.0'}, 'http_version': '1.1',
             'method': 'GET', 'scheme': 'http', 'path': '/ping', 'raw_path': b'/ping',
             'query_string': b'', 'root_path': '', 'headers': [(b'host', b'x')],
             'client': ('127.0.0.1', 5), 'server': ('localhost', 80)}

    async def receive():
        return {'type': 'http.request', 'body': b'', 'more_body': False}

    async def send(event):
        pass

    await app(dict(scope), receive, send)
    gc.collect()
    gc.disable()
    try:
        for _ in range(300):
            await app(dict(scope), receive, send)
        per_request = gc.collect() / 300
    finally:
        gc.enable()
    assert per_request < 1.0
