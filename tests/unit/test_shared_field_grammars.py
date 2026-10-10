"""One reading per field grammar, wherever the field is read.

A media type is case-insensitive and ends before its parameters (RFC 9110
§8.3.1); a list field may arrive on several lines and reads as one list
(§5.3); If-None-Match compares entity-tags weakly (§13.1.2).  Every reader
of these fields reaches the same answer.
"""
from __future__ import annotations

import pytest

from blackbull import BlackBull
from blackbull.connection import Connection
from blackbull.grpc import GrpcServiceRegistry, encode_message
from blackbull.middleware.cache import Cache
from blackbull.middleware.static import StaticFiles
from blackbull.protocol.field_grammar import (
    if_none_match_hit, list_members, media_type)


class TestTheGrammars:
    @pytest.mark.parametrize('value,expected', [
        (b'text/html', b'text/html'),
        (b'Text/HTML; charset=UTF-8', b'text/html'),
        (b'application/grpc+proto ;x=1', b'application/grpc+proto'),
        (b'', b''),
    ])
    def test_a_media_type_is_lowercase_without_parameters(self, value, expected):
        assert media_type(value) == expected

    def test_list_members_are_lowercase_without_ows_or_empties(self):
        assert list_members(b'Accept-Encoding, ,\tUser-Agent ,') == [
            b'accept-encoding', b'user-agent']

    @pytest.mark.parametrize('value,hit', [
        (b'*', True), (b'"a", W/"b"', True), (b'W/"b"', True),
        (b'"B"', False), (b'"c"', False),
    ])
    def test_if_none_match_compares_weakly_and_case_sensitively(self, value, hit):
        assert if_none_match_hit(value, b'"b"') is hit


def _scope(path, headers):
    return {'type': 'http', 'http_version': '2', 'method': 'POST',
            'scheme': 'http', 'path': path, 'raw_path': path.encode(),
            'query_string': b'', 'headers': headers}


async def _call(app, scope, body=b''):
    sent = []

    async def receive():
        return {'type': 'http.request', 'body': body, 'more_body': False}

    async def send(event):
        sent.append(event)

    await app(scope, receive, send)
    return sent


@pytest.mark.asyncio
class TestGrpcDispatchUsesTheGrpcMediaType:
    def _app(self):
        app = BlackBull()
        registry = GrpcServiceRegistry()

        @registry.method('/demo.S/M')
        async def m(request, context):
            return b'grpc'

        app.enable_grpc(registry)

        @app.route(path='/demo.S/M', methods=['POST'])
        async def plain():
            return 'http'

        return app

    @pytest.mark.parametrize('content_type', [b'application/grpc',
                                              b'Application/GRPC+proto'])
    async def test_a_grpc_media_type_reaches_grpc(self, content_type):
        sent = await _call(self._app(),
                           _scope('/demo.S/M', [(b'content-type', content_type)]),
                           encode_message(b'x'))
        assert any(e['type'] == 'http.response.trailers' for e in sent)

    async def test_grpc_web_is_not_grpc(self):
        sent = await _call(self._app(),
                           _scope('/demo.S/M', [(b'content-type', b'application/grpc-web')]))
        body = b''.join(e.get('body', b'') for e in sent
                        if e['type'] == 'http.response.body')
        assert body == b'http'


@pytest.mark.asyncio
class TestIfNoneMatchOnSeveralLines:
    async def test_static_reads_every_line(self, tmp_path):
        (tmp_path / 'f.txt').write_bytes(b'x')
        static = StaticFiles(directory=str(tmp_path))
        first = await _get(static, '/f.txt', [])
        etag = dict(first[0]['headers'])[b'etag']

        again = await _get(static, '/f.txt', [(b'if-none-match', etag),
                                              (b'if-none-match', b'"other"')])
        assert again[0]['status'] == 304

    async def test_cache_reads_every_line(self):
        cache = Cache(max_age=60)

        async def handler(conn, receive, send):
            await send({'type': 'http.response.start', 'status': 200,
                        'headers': [(b'etag', b'"v1"')]})
            await send({'type': 'http.response.body', 'body': b'x'})

        await _through(cache, handler, [])
        again = await _through(cache, handler, [(b'if-none-match', b'"other"'),
                                                (b'if-none-match', b'"v1"')])
        assert _status(again) == 304


def _get_scope(path, headers):
    return {'type': 'http', 'http_version': '1.1', 'method': 'GET',
            'scheme': 'http', 'path': path, 'raw_path': path.encode(),
            'query_string': b'', 'headers': [(b'host', b'a.example'), *headers],
            'server': ('a.example', 80)}


async def _get(asgi, path, headers):
    from blackbull.native import asgi_send_boundary
    sent = []

    async def receive():
        return {'type': 'http.request', 'body': b'', 'more_body': False}

    async def send(event):
        sent.append(event)

    await asgi(Connection.from_scope(_get_scope(path, headers)), receive,
               asgi_send_boundary(send))
    return sent


async def _through(middleware, handler, headers):
    from blackbull.native import asgi_send_boundary
    sent = []

    async def send(event):
        sent.append(event)

    conn = Connection.from_scope(_get_scope('/c', headers))
    await middleware(conn, None, asgi_send_boundary(send), handler)
    return sent


def _status(sent):
    return next(e['status'] for e in sent if e['type'] == 'http.response.start')
