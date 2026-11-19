"""Lifecycle events through a served app; the contracts are in docs/guide/events.md."""
import asyncio

import pytest
from hpack import Encoder
from websockets.asyncio.client import connect

from blackbull import BlackBull, Response, StreamingResponse
from blackbull.connection import Connection
from blackbull.event import Event
from blackbull.middleware import Compression
from blackbull.protocol.frame_types import FrameTypes, HeaderFrameFlags
from blackbull.request import read_body
from blackbull.server.access_log import AccessLogRecord
from blackbull.utils import Scheme

from tests.conformance.http2._harness import _make_h2_frame, h2_connect
from tests.harness import read_h2_frame, served

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


#: A request whose declared body never fully arrives.
_PARTIAL = b'POST /slow HTTP/1.1\r\nHost: x\r\nContent-Length: 10\r\n\r\nabc'


async def _reads_conn_body(conn, receive, send):
    await conn.body()


async def _reads_read_body(conn, receive, send):
    await read_body(receive)


async def _reads_receive(conn, receive, send):
    while (await receive())['type'] != 'http.disconnect':
        pass


async def _until(seen, name) -> None:
    for _ in range(200):
        if seen[name]:
            break
        await asyncio.sleep(0.01)
    await asyncio.sleep(SETTLE)


async def _gone_mid_body(app, seen) -> None:
    async with served(app) as s:
        w = await s.wire()
        await w.send(_PARTIAL)
        await asyncio.sleep(SETTLE)
        await w.aclose()
        await _until(seen, 'request_disconnected')


@pytest.mark.parametrize('reader', [_reads_conn_body, _reads_read_body, _reads_receive],
                         ids=['conn.body', 'read_body', 'receive'])
async def test_a_client_gone_mid_body_is_disconnected_not_completed(reader):
    app, seen = _app(**{'/slow': reader})
    await _gone_mid_body(app, seen)
    [event] = seen['request_disconnected']
    d = event.detail
    assert (d['method'], d['path'], _conn(d).path) == ('POST', '/slow', '/slow')
    assert 'client_ip' in d and d['http_version'] == '1.1'
    assert not {'status', 'response_bytes', 'duration_ms'} & d.keys()
    assert seen['request_completed'] == []


async def test_a_lone_disconnect_listener_is_told():
    app = BlackBull()
    app.route(path='/slow', methods=['POST'])(_reads_conn_body)
    got = []

    @app.on('request_disconnected')
    async def observe(event: Event):
        got.append(event)

    await _gone_mid_body(app, {'request_disconnected': got})
    assert len(got) == 1


async def test_reading_past_the_body_is_not_a_disconnect():
    async def past(conn, receive, send):
        await receive()
        assert (await receive())['type'] == 'http.disconnect'
        await send(Response(b'ok'))

    app, seen = _app(**{'/past': past})
    await _http(app, _get(b'/past', method=b'POST', body=b'abc'))
    assert seen['request_disconnected'] == []
    assert len(seen['request_completed']) == 1


async def test_a_client_gone_mid_response_is_disconnected_not_completed():
    async def chunks():
        for _ in range(400):
            yield b'x' * 65536

    async def big(conn, receive, send):
        await StreamingResponse(chunks())(conn, receive, send)

    app, seen = _app(**{'/big': big})
    async with served(app) as s:
        w = await s.wire()
        await w.send(_get(b'/big'))
        await w.recv_until()
        await w.aclose()
        await _until(seen, 'request_disconnected')
    assert len(seen['request_disconnected']) == 1
    assert seen['request_completed'] == []


async def _h2_reset(app, seen, *, body: bool) -> None:
    fields = [(b':method', b'POST'), (b':path', b'/slow'), (b':scheme', b'http'),
              (b':authority', b'x')]
    end = 0 if body else HeaderFrameFlags.END_STREAM
    async with served(app) as s:
        wire = await h2_connect(s)
        frames = _make_h2_frame(FrameTypes.HEADERS, HeaderFrameFlags.END_HEADERS | end, 1,
                                Encoder().encode(fields))
        if body:
            frames += _make_h2_frame(FrameTypes.DATA, 0, 1, b'abc')
        await wire.send(frames)
        await asyncio.sleep(SETTLE)
        await wire.send(_make_h2_frame(FrameTypes.RST_STREAM, 0, 1, (8).to_bytes(4, 'big')))
        await _until(seen, 'request_disconnected')


async def test_an_h2_reset_mid_body_is_disconnected_not_completed():
    app, seen = _app(**{'/slow': _reads_conn_body})
    await _h2_reset(app, seen, body=True)
    [event] = seen['request_disconnected']
    assert (event.detail['path'], event.detail['http_version']) == ('/slow', '2')
    assert seen['request_completed'] == []


async def test_a_stream_answered_after_the_clients_goaway_is_completed():
    async def late(conn, receive, send):
        await asyncio.sleep(SETTLE)
        await send(Response(b'late'))

    app, seen = _app(**{'/late': late})
    fields = [(b':method', b'GET'), (b':path', b'/late'), (b':scheme', b'http'),
              (b':authority', b'x')]
    async with served(app) as s:
        wire = await h2_connect(s)
        await wire.send(_make_h2_frame(
            FrameTypes.HEADERS, HeaderFrameFlags.END_HEADERS | HeaderFrameFlags.END_STREAM, 1,
            Encoder().encode(fields)))
        await wire.send(_make_h2_frame(FrameTypes.GOAWAY, 0, 0, bytes(8)))
        body = b''
        while True:
            kind, flags, sid, payload = await read_h2_frame(wire.reader)
            if kind == 0 and sid == 1:
                body += payload
                if flags & 1:
                    break
        await _until(seen, 'request_completed')
    assert body == b'late'
    assert len(seen['request_completed']) == 1 and seen['request_disconnected'] == []


async def test_a_handler_cancelled_by_an_h2_reset_sees_the_peer_gone():
    seen_gone = []

    async def waits(conn, receive, send):
        try:
            await asyncio.Event().wait()
        finally:
            seen_gone.append(conn.disconnected)

    app, seen = _app(**{'/slow': waits})
    await _h2_reset(app, seen, body=False)
    assert seen_gone == [True]
    assert len(seen['request_disconnected']) == 1 and seen['request_completed'] == []


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
