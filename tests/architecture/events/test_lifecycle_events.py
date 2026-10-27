"""Lifecycle events through a served app; the contracts are in docs/guide/events.md."""
import asyncio

import pytest
from websockets.asyncio.client import connect

from blackbull import BlackBull, StreamingResponse
from blackbull.connection import Connection
from blackbull.event import Event
from blackbull.middleware import Compression
from blackbull.server.access_log import AccessLogRecord
from blackbull.utils import Scheme

from tests.harness import served

pytestmark = pytest.mark.asyncio

HTTP_EVENTS = ('request_received', 'before_handler', 'after_handler', 'request_completed')
WS_EVENTS = ('websocket_connected', 'websocket_disconnected')
ALL = HTTP_EVENTS + ('request_disconnected',) + WS_EVENTS
SETTLE = 0.05


def _get(path=b'/hello', *fields, method=b'GET', body=b''):
    if body:
        fields += (b'Content-Length: %d' % len(body),)
    return b'\r\n'.join([method + b' ' + path + b' HTTP/1.1', b'Host: x', *fields]) + b'\r\n\r\n' + body


def _app(**routes) -> tuple[BlackBull, dict[str, list[Event]]]:
    app = BlackBull()
    seen: dict[str, list[Event]] = {name: [] for name in ALL}
    for name in ALL:
        @app.on(name)
        async def observe(event: Event, _name=name):
            seen[_name].append(event)

    async def ok(conn, receive, send):
        await send({'type': 'http.response.start', 'status': 200, 'headers': []})
        await send({'type': 'http.response.body', 'body': b'ok'})

    for path, handler in {'/hello': ok, **routes}.items():
        app.route(path=path, methods=['GET', 'POST', 'QUERY'])(handler)
    return app, seen


async def _http(app, *requests) -> None:
    async with served(app) as s:
        w = await s.wire()
        for raw in requests:
            await w.send(raw)
            await w.response()
        await asyncio.sleep(SETTLE)


async def _ws(app, path='/ws', **kw) -> None:
    async with served(app) as s:
        async with connect(f'ws://127.0.0.1:{s.server.port}{path}', **kw) as ws:
            await ws.wait_closed()
        await asyncio.sleep(SETTLE)


def _conn(detail) -> Connection:
    """The detail's connection; a scope dict on the forced-ASGI lane."""
    c = detail['conn']
    return c if isinstance(c, Connection) else Connection.from_scope(c)


def _counts(seen) -> dict[str, int]:
    return {name: len(events) for name, events in seen.items() if events}


@pytest.mark.parametrize('requests,n', [
    ((_get(),), 1),
    ((_get(),) * 3, 3),
    ((_get(b'/hello', method=b'QUERY', body=b'select *'),), 1),
], ids=['one', 'keep-alive-three', 'query-method'])
async def test_every_http_lifecycle_event_fires_once_per_request(requests, n):
    app, seen = _app()
    await _http(app, *requests)
    assert _counts(seen) == dict.fromkeys(HTTP_EVENTS, n)
    assert seen['request_received'][0].detail['method'] == requests[0].split()[0].decode()


async def test_lifecycle_details_describe_the_request_and_response():
    app, seen = _app()
    await _http(app, _get())
    received, before, after, completed = (seen[n][0].detail for n in HTTP_EVENTS)
    for d in (received, before, after):
        assert (d['method'], d['path'], d['client_ip']) == ('GET', '/hello', '127.0.0.1')
        assert d['conn'].path == '/hello'
    assert received['headers'].get(b'host') == b'x' and received['http_version'] == '1.1'
    assert isinstance(before['handler'], str) and after['exception'] is None
    conn = completed['conn']
    assert (completed['method'], completed['path'], completed['http_version']) == \
        (conn.method, conn.path, conn.http_version)
    assert (completed['status'], completed['response_bytes']) == (200, 2)
    assert isinstance(completed['duration_ms'], float) and completed['duration_ms'] >= 0


async def test_a_raising_handler_still_completes_with_500():
    async def boom(conn, receive, send):
        raise ValueError('intentional')

    app, seen = _app(**{'/boom': boom})
    await _http(app, _get(b'/boom'))
    assert isinstance(seen['after_handler'][0].detail['exception'], ValueError)
    assert seen['request_completed'][0].detail['status'] == 500
    assert _counts(seen) == dict.fromkeys(HTTP_EVENTS, 1)


async def test_an_exception_escaping_the_app_is_an_error_event():
    app, _ = _app()
    errors = []

    @app.on('error')
    async def on_error(event: Event):
        errors.append(event.detail)

    async def boom(scope, receive, send, call_next):
        raise RuntimeError('middleware')

    app.use(boom)
    async with served(app) as s:
        w = await s.wire()
        await w.send(_get())
        assert await w.closed()
        await asyncio.sleep(SETTLE)
    [d] = errors
    assert _conn(d).path == '/hello' and isinstance(d['exception'], RuntimeError)


async def test_an_unrouted_request_completes_with_404():
    app, seen = _app()
    await _http(app, _get(b'/nowhere'))
    assert [(e.detail['status'], e.detail['path']) for e in seen['request_completed']] == \
        [(404, '/nowhere')]


@pytest.mark.parametrize('hooks,expected', [
    (('after_handler', 'request_completed'), ['handler', 'after_handler', 'request_completed']),
    (('request_received', 'before_handler'), ['request_received', 'before_handler', 'handler']),
])
async def test_interceptors_run_in_lifecycle_order(hooks, expected):
    order: list[str] = []

    async def handler(conn, receive, send):
        order.append('handler')
        await send({'type': 'http.response.start', 'status': 200, 'headers': []})
        await send({'type': 'http.response.body', 'body': b''})

    app = BlackBull()
    app.route(path='/order')(handler)
    for hook in hooks:
        @app.intercept(hook)
        async def record(event: Event, _hook=hook):
            order.append(_hook)

    await _http(app, _get(b'/order'))
    assert order == expected


async def test_a_raising_before_handler_interceptor_skips_the_handler():
    ran = []

    async def handler(conn, receive, send):
        ran.append(True)

    app, _ = _app(**{'/guarded': handler})

    @app.intercept('before_handler')
    async def guard(event: Event):
        raise PermissionError('blocked')

    async with served(app) as s:
        w = await s.wire()
        await w.send(_get(b'/guarded'))
        assert (await w.response()).status >= 400
    assert ran == []


async def test_completion_reports_what_buffering_middleware_sent():
    body = b'<html>' + b'hello world ' * 50 + b'</html>'

    async def page(conn, receive, send):
        await send({'type': 'http.response.start', 'status': 200,
                    'headers': [(b'content-type', b'text/html')]})
        await send({'type': 'http.response.body', 'body': body})

    app, seen = _app(**{'/gz': page})
    app.use(Compression())
    await _http(app, _get(b'/gz', b'Accept-Encoding: gzip'))
    detail = seen['request_completed'][0].detail
    assert detail['status'] == 200 and 0 < detail['response_bytes'] < len(body)


async def test_completion_follows_the_last_streamed_chunk():
    async def chunks():
        for c in (b'chunk0', b'chunk1', b'chunk2'):
            yield c

    async def stream(conn, receive, send):
        await StreamingResponse(chunks())(conn, receive, send)

    async def declared(conn, receive, send):
        await send({'type': 'http.response.start', 'status': 200,
                    'headers': [(b'content-length', b'4')]})
        await send({'type': 'http.response.body', 'body': b'ab', 'more_body': True})
        await send({'type': 'http.response.body', 'body': b'cd'})

    app, seen = _app(**{'/stream': stream, '/declared': declared})
    await _http(app, _get(b'/stream'), _get(b'/declared'))
    assert [(e.detail['status'], e.detail['response_bytes'])
            for e in seen['request_completed']] == [(200, 18), (200, 4)]


async def test_a_lone_disconnect_listener_is_told():
    async def waits(conn, receive, send):
        await receive()
        await receive()

    app = BlackBull()
    app.route(path='/slow')(waits)
    got = []

    @app.on('request_disconnected')
    async def observe(event: Event):
        got.append(event)

    async with served(app) as s:
        w = await s.wire()
        await w.send(_get(b'/slow'))
        for _ in range(200):
            if got:
                break
            await asyncio.sleep(0.01)
    assert len(got) == 1


async def test_a_handler_that_finds_the_peer_gone_is_disconnected_not_completed():
    async def waits(conn, receive, send):
        await receive()
        await receive()

    app, seen = _app(**{'/slow': waits})
    async with served(app) as s:
        w = await s.wire()
        await w.send(_get(b'/slow'))
        for _ in range(200):
            if seen['request_disconnected']:
                break
            await asyncio.sleep(0.01)
        await asyncio.sleep(SETTLE)
    [event] = seen['request_disconnected']
    d = event.detail
    assert (d['method'], d['path'], _conn(d).path) == ('GET', '/slow', '/slow')
    assert 'client_ip' in d and 'http_version' in d
    assert not {'status', 'response_bytes', 'duration_ms'} & d.keys()
    assert seen['request_completed'] == []


async def test_an_answered_request_is_not_disconnected():
    app, seen = _app()
    await _http(app, _get())
    assert seen['request_disconnected'] == []


# -- WebSocket sessions ---------------------------------------------------

def _ws_handler(subprotocol=None, *, echo=False):
    async def handler(conn, receive, send):
        await receive()
        await send({'type': 'websocket.accept', 'subprotocol': subprotocol})
        if echo:
            await receive()
        await send({'type': 'websocket.close'})
    return handler


def _ws_app(**kw):
    app, seen = _app()
    app.route(path='/ws', scheme=Scheme.websocket)(_ws_handler(**kw))
    return app, seen


async def test_a_session_fires_only_the_websocket_events_once():
    app, seen = _ws_app()
    await _ws(app)
    assert _counts(seen) == dict.fromkeys(WS_EVENTS, 1)
    d = seen['websocket_connected'][0].detail
    assert d['path'] == '/ws' and d['subprotocol'] is None and isinstance(d['client_ip'], str)
    assert isinstance(d['connection_id'], str) and d['connection_id'] and 'conn' in d
    gone = seen['websocket_disconnected'][0].detail
    assert set(gone) == {'conn', 'connection_id', 'client_ip', 'path', 'code'}
    assert gone['connection_id'] == d['connection_id'] and gone['path'] == '/ws'
    assert isinstance(gone['code'], int)


async def test_the_accepted_subprotocol_is_reported():
    app, seen = _ws_app(subprotocol='chat')
    await _ws(app, subprotocols=['chat'])
    assert seen['websocket_connected'][0].detail['subprotocol'] == 'chat'


async def test_a_message_names_the_connection_its_session_was_given():
    app, seen = _ws_app(echo=True)
    message_ids = []

    @app.on('websocket_message')
    async def on_message(event: Event):
        message_ids.append(event.detail['conn'].connection_id)

    async with served(app) as s:
        async with connect(f'ws://127.0.0.1:{s.server.port}/ws') as ws:
            await ws.send('hello')
            await ws.wait_closed()
        await asyncio.sleep(SETTLE)
    [connected] = seen['websocket_connected']
    assert message_ids == [connected.detail['connection_id']]


# Fires when the server reads a message, whether or not the handler consumes it.
WS_MESSAGES = [
    ('text', ['hello'], True, [('hello', None)]),
    ('binary', [b'\x00\x01\x02'], True, [(None, b'\x00\x01\x02')]),
    ('fragmented', [['hel', 'lo']], True, [('hello', None)]),
    ('fragmented-empty-final', [['hel', 'lo', '']], True, [('hello', None)]),
    ('several', ['first', 'second', 'third'], True,
     [('first', None), ('second', None), ('third', None)]),
    ('unread', ['m0', 'm1', 'm2'], False, [('m0', None), ('m1', None), ('m2', None)]),
]


@pytest.mark.parametrize('sent,reads,expected', [row[1:] for row in WS_MESSAGES],
                         ids=[row[0] for row in WS_MESSAGES])
async def test_each_websocket_message_is_one_event(sent, reads, expected):
    app = BlackBull()
    details: list[dict] = []
    done = asyncio.Event()

    @app.on('websocket_message')
    async def on_message(event: Event):
        details.append(event.detail)
        if len(details) == len(expected):
            done.set()

    @app.route(path='/ws', scheme=Scheme.websocket)
    async def ws(conn, receive, send):
        await receive()
        await send({'type': 'websocket.accept'})
        if reads:
            for _ in sent:
                await receive()
            await send({'type': 'websocket.close'})
        else:
            await asyncio.sleep(1.5)        # never consumes; bounded

    async with served(app) as s:
        async with connect(f'ws://127.0.0.1:{s.server.port}/ws') as client:
            for message in sent:
                await client.send(message)
            await asyncio.wait_for(done.wait(), 3.0)
    assert [(d['text'], d['bytes']) for d in details] == expected
    assert all(set(d) == {'conn', 'text', 'bytes'} for d in details)


async def test_a_listener_registered_mid_session_is_told():
    """The listener check runs per message, not once per connection."""
    app = BlackBull()
    fired = asyncio.Event()

    @app.route(path='/ws', scheme=Scheme.websocket)
    async def ws(conn, receive, send):
        await receive()
        await send({'type': 'websocket.accept'})
        await receive()
        await send({'type': 'websocket.close'})

    async with served(app) as s:
        async with connect(f'ws://127.0.0.1:{s.server.port}/ws') as client:
            @app.on('websocket_message')
            async def on_message(event: Event):
                fired.set()
            await client.send('late')
            await asyncio.wait_for(fired.wait(), 3.0)


async def test_a_raising_websocket_handler_is_an_error_event():
    async def boom(conn, receive, send):
        await receive()
        await send({'type': 'websocket.accept'})
        raise RuntimeError('ws handler')

    app, _ = _app()
    app.route(path='/ws', scheme=Scheme.websocket)(boom)
    errors = []

    @app.on('error')
    async def on_error(event: Event):
        errors.append(event.detail)

    await _ws(app)
    [d] = errors
    assert _conn(d).path == '/ws' and isinstance(d['exception'], RuntimeError)


async def test_an_http_request_fires_no_websocket_event():
    app, seen = _app()
    await _http(app, _get())
    assert not any(seen[n] for n in WS_EVENTS)


# -- the external ASGI entry (one dispatch per HTTP/2 stream) -------------

@pytest.mark.parametrize('client', [['10.0.0.1', 0], None])
async def test_an_external_dispatch_reports_the_record_the_host_left_in_state(client):
    app, seen = _app()
    record = AccessLogRecord(client_ip='10.0.0.1', method='GET', path='/hello',
                             http_version='2', status=200, response_bytes=42)
    scope = {'type': 'http', 'method': 'GET', 'path': '/hello', 'http_version': '2',
             'client': client, 'server': None, 'headers': [], 'state': {'access_log': record}}

    async def receive():
        return {'type': 'http.request', 'body': b'', 'more_body': False}

    async def send(event, *a, **kw):
        pass

    await app(scope, receive, send)
    await asyncio.sleep(SETTLE)
    d = seen['request_completed'][0].detail
    assert (d['path'], d['status'], d['response_bytes']) == ('/hello', 200, 42)
