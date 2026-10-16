"""What an application's send events become on an HTTP/1.1 connection."""
import asyncio

import pytest

from blackbull import BlackBull
from blackbull.env import reset_settings_cache
from blackbull.native import NativeWSMessage

from tests.harness import served

pytestmark = pytest.mark.asyncio

START = {'type': 'http.response.start', 'status': 200, 'headers': []}


def _start(status=200, headers=(), **kw):
    return {'type': 'http.response.start', 'status': status, 'headers': list(headers), **kw}


def _body(body=b'', more=False):
    return {'type': 'http.response.body', 'body': body, 'more_body': more}


def _trailers(*headers, more=False):
    return {'type': 'http.response.trailers', 'headers': list(headers), 'more_trailers': more}


def _replay(*events):
    async def app(conn, receive, send):
        for event in events:
            await send(*event) if isinstance(event, tuple) else await send(event)
    return app


async def _wire(app, request=b'GET / HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n') -> bytes:
    async with served(app) as s:
        w = await s.wire()
        await w.send(request)
        return await w.recv_all()


def _split(raw: bytes) -> tuple[bytes, bytes]:
    head, _, body = raw.partition(b'\r\n\r\n')
    return head.lower(), body


@pytest.mark.parametrize('events,status_line', [
    ((START, _body()), b'http/1.1 200 ok'),
    ((_start(404), _body()), b'http/1.1 404 not found'),
])
async def test_the_status_line_carries_code_and_reason(events, status_line):
    head, _ = _split(await _wire(_replay(*events)))
    assert head.split(b'\r\n')[0] == status_line


async def test_headers_and_body_reach_the_wire():
    head, body = _split(await _wire(_replay(
        _start(headers=[(b'content-type', b'text/plain'), (b'content-length', b'5')]),
        _body(b'hello'))))
    assert b'\r\ncontent-type: text/plain' in head and body == b'hello'
    assert head.count(b'content-length:') == 1


async def test_content_length_and_date_are_supplied():
    head, body = _split(await _wire(_replay(START, _body(b'hello world'))))
    assert b'\r\ncontent-length: 11' in head and b'\r\ndate: ' in head
    assert body == b'hello world'


@pytest.mark.parametrize('name', [b'content-length', b'Content-Length', b'CONTENT-LENGTH'])
async def test_an_app_content_length_is_not_repeated_in_any_case(name):
    from http import HTTPStatus
    head, _ = _split(await _wire(_replay((b'hello', HTTPStatus.OK, [(name, b'5')]))))
    assert head.count(b'content-length:') == 1


TRAILERS = {
    'one-trailer': ((_start(trailers=True), _body(b'hello'), _trailers((b'x-checksum', b'abc123'))),
                    b'5\r\nhello\r\n0\r\nx-checksum: abc123\r\n\r\n'),
    'declared-length-becomes-chunked': (
        (_start(headers=[(b'content-length', b'5')], trailers=True), _body(b'hello'),
         _trailers((b'x-checksum', b'abc123')), _body(b'extra')),
        b'5\r\nhello\r\n0\r\nx-checksum: abc123\r\n\r\n'),
    'empty-body': ((_start(trailers=True), _body(), _trailers((b'x-empty', b'yes'))),
                   b'0\r\nx-empty: yes\r\n\r\n'),
    'streamed-body': ((_start(trailers=True), _body(b'a', True), _body(b'b'),
                       _trailers((b'x-stream', b'done'))),
                      b'1\r\na\r\n1\r\nb\r\n0\r\nx-stream: done\r\n\r\n'),
    'two-trailer-events': ((_start(trailers=True), _body(b'ok'), _trailers((b'x-first', b'1'), more=True),
                            _trailers((b'x-second', b'2'))),
                           b'2\r\nok\r\n0\r\nx-first: 1\r\nx-second: 2\r\n\r\n'),
}


@pytest.mark.parametrize('events,body', TRAILERS.values(), ids=TRAILERS.keys())
async def test_trailers_close_a_chunked_body(events, body):
    head, wire_body = _split(await _wire(_replay(*events)))
    assert b'\r\ntransfer-encoding: chunked' in head and b'content-length' not in head
    assert wire_body == body


async def test_head_sends_neither_body_nor_trailers():
    head, body = _split(await _wire(
        _replay(_start(headers=[(b'content-length', b'5')], trailers=True), _body(b'hello'),
                _trailers((b'x-checksum', b'abc123'))),
        b'HEAD / HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n'))
    assert (body, b'x-checksum' in head) == (b'', False)


@pytest.mark.parametrize('events', [
    ((b'first',), (b'second', 500)),
    (START, _body(b'first'), _start(500), _body(b'second')),
], ids=['bytes', 'events'])
async def test_a_completed_response_is_never_followed_by_another(events):
    raw = await _wire(_replay(*events))
    assert raw.count(b'HTTP/1.1 ') == 1 and b'first' in raw and b'second' not in raw


@pytest.mark.parametrize('version,interim', [(b'HTTP/1.1', True), (b'HTTP/1.0', False)])
async def test_an_interim_response_goes_only_to_a_peer_that_can_read_it(version, interim):
    raw = await _wire(_replay(_start(103), _start(200, [(b'content-length', b'2')]), _body(b'ok')),
                      b'GET / ' + version + b'\r\nHost: x\r\nConnection: close\r\n\r\n')
    assert raw.startswith(b'HTTP/1.1 103 ' if interim else b'HTTP/1.1 200 ')
    assert (b' 103 ' in raw) is interim


async def _calls_on_a_reused_connection(app_events, *, cancel_after=None) -> tuple[list, bytes]:
    calls = []

    async def app(conn, receive, send):
        calls.append(conn.path)
        for event in app_events:
            if isinstance(event, BaseException):
                raise event
            await send(event)

    async with served(app) as s:
        w = await s.wire()
        await w.send(b'GET /one HTTP/1.1\r\nHost: x\r\n\r\nGET /two HTTP/1.1\r\nHost: x\r\n\r\n')
        raw = await w.recv_all()
    return calls, raw


UNREUSABLE = {
    'invalid-response-field': (_start(headers=[(b'x-origin', b'a\r\nx-added: b')]), _body()),
    'short-body-no-length': (START, _body(b'a', True)),
    'short-body-declared': (_start(headers=[(b'content-length', b'2')]), _body(b'a', True)),
    'start-only': (START,),
    'interim-then-body': (_start(103), _body()),
    'trailers-promised-not-sent': (_start(trailers=True), _body(b'ok')),
    'raise-before-trailers': (_start(trailers=True), _body(b'ok'), RuntimeError('boom')),
}


@pytest.mark.parametrize('events', UNREUSABLE.values(), ids=UNREUSABLE.keys())
async def test_an_unfinished_or_invalid_response_ends_the_connection(events):
    calls, raw = await _calls_on_a_reused_connection(events)
    assert calls == ['/one'] and b'x-added:' not in raw


@pytest.mark.parametrize('failure', ['short-body', 'handler-error'])
async def test_a_failure_after_the_start_is_not_followed_by_an_error_response(failure):
    app = BlackBull()

    @app.route(path='/')
    async def partial(conn, receive, send):
        await send(_start(headers=[(b'content-length', b'2')]))
        await send(_body(b'a', True))
        if failure == 'short-body':
            await send(_body())
        else:
            raise RuntimeError('after start')

    raw = await _wire(app)
    assert raw.count(b'HTTP/1.1 ') == 1 and raw.startswith(b'HTTP/1.1 200 OK\r\n')


async def test_a_request_timeout_cancels_a_handler_holding_trailers(monkeypatch):
    monkeypatch.setenv('BB_REQUEST_TIMEOUT', '0.05')
    reset_settings_cache()
    cancelled = asyncio.Event()

    async def app(conn, receive, send):
        await send(_start(trailers=True))
        await send(_body(b'ok'))
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    try:
        await _wire(app)
    finally:
        reset_settings_cache()
    assert cancelled.is_set()


# -- the 101 that answers a WebSocket upgrade -----------------------------

def _upgrade(*extra):
    return b'\r\n'.join([b'GET /ws HTTP/1.1', b'Host: x', b'Upgrade: websocket',
                         b'Connection: Upgrade', b'Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==',
                         b'Sec-WebSocket-Version: 13', *extra]) + b'\r\n\r\n'


async def _accept_head(accept, request=None, *, to_eof=False) -> list[bytes]:
    async def app(conn, receive, send):
        await receive()
        await send(accept)
        await receive()

    async with served(app) as s:
        w = await s.wire()
        await w.send(request or _upgrade())
        raw = await (w.recv_all() if to_eof else w.recv_until())
        return raw.split(b'\r\n\r\n')[0].split(b'\r\n')


ACCEPT = {'type': 'websocket.accept', 'subprotocol': None}


@pytest.mark.parametrize('accept,wanted', [
    ({**ACCEPT, 'headers': [[b'x-review', b'ok']]}, [b'x-review: ok']),
    (NativeWSMessage.accept(None, [(b'x-review', b'ok')]), [b'x-review: ok']),
    ({**ACCEPT, 'headers': [[b'set-cookie', b'a=1'], [b'x-mid', b'm'], [b'set-cookie', b'b=2']]},
     [b'set-cookie: a=1', b'x-mid: m', b'set-cookie: b=2']),
], ids=['asgi', 'native', 'repeated-fields-keep-order'])
async def test_accept_headers_reach_the_101(accept, wanted):
    lines = await _accept_head(accept)
    assert lines[0].startswith(b'HTTP/1.1 101 ')
    assert [ln for ln in lines if ln in wanted] == wanted


async def test_a_bare_accept_sends_protocol_fields_only():
    assert len(await _accept_head(ACCEPT)) == 5   # status, upgrade, connection, accept, date


async def test_subprotocol_extension_and_headers_combine():
    lines = await _accept_head(
        {**ACCEPT, 'subprotocol': 'chat', 'headers': [[b'x-review', b'ok']]},
        _upgrade(b'Sec-WebSocket-Protocol: chat', b'Sec-WebSocket-Extensions: permessage-deflate'))
    for want in (b'sec-websocket-protocol: chat', b'sec-websocket-extensions: permessage-deflate',
                 b'x-review: ok'):
        assert want in lines


@pytest.mark.parametrize('header', [[b'sec-websocket-accept', b'evil'],
                                    [b'x-review', b'ok\r\ninjected: 1']])
async def test_an_accept_header_that_would_corrupt_the_101_is_refused(header):
    lines = await _accept_head({**ACCEPT, 'headers': [header]}, to_eof=True)
    assert not lines[0].startswith(b'HTTP/1.1 101 ')
    assert not any(b'evil' in ln or b'injected' in ln for ln in lines)
