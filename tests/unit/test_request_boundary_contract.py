"""Every Connection a boundary builds carries the same header contract.

Native parsing and ``Connection.from_scope`` (an external ASGI host, or
``BB_FORCE_ASGI_SCOPE=1``) both hand dispatch a Connection whose header names
are lowercase tchar, whose values carry no edge SP/HTAB and no CTL, whose Host
is at most one valid authority, and whose scheme is lowercase.  Code past
``app._dispatch_http`` relies on that and does not normalise again.
"""
from __future__ import annotations

import pytest

from blackbull import BlackBull
from blackbull.connection import Connection
from blackbull.native import asgi_send_boundary
from blackbull.protocol.field_grammar import FieldError
from blackbull.server.http1_actor import BadRequestError, HTTP1Actor


def _scope(headers, **extra) -> dict:
    scope = {'type': 'http', 'http_version': '1.1', 'method': 'GET',
             'scheme': 'http', 'path': '/', 'raw_path': b'/',
             'query_string': b'', 'headers': list(headers)}
    scope.update(extra)
    return scope


def _native(field_line: bytes):
    """Headers of the native HTTP/1.1 parse, or the refusal."""
    raw = (b'GET / HTTP/1.1\r\nHost: example.com\r\n' + field_line
           + b'\r\n\r\n')
    try:
        return list(object.__new__(HTTP1Actor)._parse(raw).headers)
    except BadRequestError:
        return 'refused'


def _from_scope(field_line: bytes):
    """Headers of from_scope fed the line as a host would split it, or the refusal."""
    name, _, value = field_line.partition(b':')
    try:
        conn = Connection.from_scope(
            _scope([(b'Host', b'example.com'), (name, value)]))
    except FieldError:
        return 'refused'
    return list(conn.headers)


class TestFromScopeKeepsTheNativeContract:
    def test_names_are_lowercase_when_iterated(self):
        conn = Connection.from_scope(_scope([(b'X-Request-ID', b'1')]))

        assert list(conn.headers) == [(b'x-request-id', b'1')]
        assert conn.headers.get(b'x-request-id') == b'1'

    def test_edge_whitespace_is_trimmed_and_inner_kept(self):
        conn = Connection.from_scope(_scope([(b'x-a', b' \tb  c\t ')]))

        assert conn.headers.get(b'x-a') == b'b  c'

    @pytest.mark.parametrize('value', [b'a\x00b', b'\x0bchunked', b'chunked\x0c',
                                       b'a\rb', b'a\nb', b'a\x7fb'])
    def test_a_control_in_a_value_is_refused(self, value):
        with pytest.raises(FieldError):
            Connection.from_scope(_scope([(b'x-a', value)]))

    @pytest.mark.parametrize('name', [b'', b'x y', b'x:y', b'x\xff', b'(x)'])
    def test_a_name_outside_tchar_is_refused(self, name):
        with pytest.raises(FieldError):
            Connection.from_scope(_scope([(name, b'v')]))

    @pytest.mark.parametrize('field', [('x-a', b'v'), (b'x-a', 'v'),
                                       (bytearray(b'x-a'), b'v'),
                                       (b'x-a', b'v', b'w'), (b'x-a',)])
    def test_a_field_that_is_not_two_bytes_strings_is_refused(self, field):
        with pytest.raises(FieldError):
            Connection.from_scope(_scope([field]))

    @pytest.mark.parametrize('hosts', [
        [b'a.example', b'b.example'],
        [b'a b'], [b'a/b'], [b''], [b'[::1'], [b'a.example:8x'],
    ])
    def test_a_repeated_or_invalid_host_is_refused(self, hosts):
        with pytest.raises(FieldError):
            Connection.from_scope(_scope([(b'host', h) for h in hosts]))

    @pytest.mark.parametrize('host', [b'a.example', b'a.example:8080',
                                      b'[::1]:80', b'192.0.2.1'])
    def test_a_valid_host_is_kept(self, host):
        conn = Connection.from_scope(_scope([(b'host', host)]))

        assert conn.headers.get(b'host') == host

    def test_the_scheme_is_lowercase(self):
        conn = Connection.from_scope(_scope([], scheme='HTTPS'))

        assert conn.scheme == 'https'

    @pytest.mark.parametrize('line', [
        b'X-Foo: bar', b'X-Foo:  bar \t', b'x-foo: a  b', b'X-Foo: caf\xc3\xa9',
        b'X-Foo: \x0bbar', b'X-Foo: bar\x0c', b'X-Foo: b\x00r', b'Foo Bar: x',
        b'Host: other.example',
    ])
    def test_the_same_line_gives_the_native_result(self, line):
        assert _from_scope(line) == _native(line)


@pytest.mark.asyncio
class TestTheAppRefusesAScopeThatBreaksTheContract:
    async def test_an_http_scope_gets_a_plain_400(self):
        app = BlackBull()
        called = []

        @app.route(path='/')
        async def root():
            called.append(True)
            return 'ok'

        sent = []

        async def receive():
            return {'type': 'http.request', 'body': b'', 'more_body': False}

        async def send(event):
            sent.append(event)

        await app(_scope([(b'host', b'a'), (b'x-a', b'\x0bchunked')]), receive, send)

        assert called == []
        assert sent[0]['type'] == 'http.response.start'
        assert sent[0]['status'] == 400

    async def test_a_websocket_scope_is_closed_before_accept(self):
        app = BlackBull()
        sent = []

        async def receive():
            return {'type': 'websocket.connect'}

        async def send(event):
            sent.append(event)

        scope = _scope([(b'host', b'a'), (b'x a', b'1')], type='websocket',
                       scheme='ws')
        await app(scope, receive, send)

        assert [e['type'] for e in sent] == ['websocket.close']
        assert sent[0]['code'] == 1002


def _router_with_routes():
    from http import HTTPMethod

    from blackbull.router import Router
    from blackbull.utils import Scheme

    router = Router()
    called = []

    @router.route(path='plain', methods=[HTTPMethod.GET])
    async def plain():
        called.append('plain')

    @router.route(path='query', methods=[HTTPMethod.GET])
    async def query(q: int = 0):
        called.append('query')

    @router.route(path='item/{id_}', methods=[HTTPMethod.GET])
    async def item(id_: str):
        called.append('item')

    def route(path):
        return router[(path, HTTPMethod.GET, Scheme.http)]

    return route, called


@pytest.mark.asyncio
@pytest.mark.parametrize('path', ['plain', 'query', 'item/1'])
async def test_a_route_driven_with_a_bare_scope_refuses_it_like_the_app(path):
    route, called = _router_with_routes()
    sent = []

    async def send(event):
        sent.append(event)

    await route(path)(_scope([(b'host', b'a'), (b'x-a', b'\x0bchunked')]), None,
                      asgi_send_boundary(send))

    assert called == []
    assert (sent[0]['type'], sent[0]['status']) == ('http.response.start', 400)
    assert b''.join(e.get('body', b'') for e in sent[1:]) == b''
