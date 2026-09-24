"""Path boundaries survive HTTP/2 dispatch, push and Extended CONNECT."""
from unittest.mock import AsyncMock

import pytest

from blackbull import BlackBull
from blackbull.connection import Connection
from .test_http2_dispatch import _make_h2_actor, _make_headers_frame
from .test_rfc8441 import (
    _client_settings, _make_extended_connect_frame, _make_normal_connect_frame,
    _make_h2_actor as _make_ws_actor,
)


@pytest.mark.asyncio
@pytest.mark.parametrize('force_asgi', [False, True])
@pytest.mark.parametrize('target,expected,wrong', [
    ('//group/review?q=ok', '//group/review', '/review'),
    ('/a%252Fb?x=%2F', '/a%2Fb', '/a/b'),
])
async def test_request_routes_without_reinterpreting_target(monkeypatch, force_asgi, target, expected, wrong):
    monkeypatch.setenv('BB_FORCE_ASGI_SCOPE', str(int(force_asgi)))
    app = BlackBull()
    seen = []

    @app.route(path=expected)
    async def wanted(conn):
        scope = conn.as_scope()
        seen.append(('wanted', conn.path, conn.raw_path, conn.query_string,
                     conn.server, conn.headers.get(b'host'),
                     (scope['path'], scope['raw_path'], scope['query_string'], scope['server'])))
        return 'wanted'

    @app.route(path=wrong)
    async def shortened(conn):
        seen.append(('wrong', conn.path, conn.raw_path, conn.query_string))
        return 'wrong'

    actor, _ = _make_h2_actor(app=app)
    actor._sockname = ('127.0.0.1', 8443)
    actor.receive = AsyncMock(side_effect=[
        _make_headers_frame(path=target.encode(), end_stream=True), None])
    await actor.run()
    raw, _, query = target.partition('?')
    assert seen == [('wanted', expected, raw.encode(), query.encode(),
                     ('127.0.0.1', 8443), b'example.com',
                     (expected, raw.encode(), query.encode(), ['127.0.0.1', 8443]))]


@pytest.mark.asyncio
@pytest.mark.parametrize('force_asgi', [False, True])
async def test_push_delivers_path_components(monkeypatch, force_asgi):
    monkeypatch.setenv('BB_FORCE_ASGI_SCOPE', str(int(force_asgi)))
    seen = []

    async def app(conn, receive, send):
        scope = conn.as_scope() if isinstance(conn, Connection) else conn
        if scope['path'] == '/':
            await send({'type': 'http.response.push', 'path': '//group/a%252Fb;v=1?x=%2F'})
        else:
            seen.append((scope['path'], scope['raw_path'], scope['query_string']))
        await send({'type': 'http.response.start', 'status': 200, 'headers': []})
        await send({'type': 'http.response.body', 'body': b''})

    actor, _ = _make_h2_actor(app=app)
    actor.receive = AsyncMock(side_effect=[_make_headers_frame(end_stream=True), None])
    await actor.run()
    assert seen == [('//group/a%2Fb;v=1', b'//group/a%252Fb;v=1', b'x=%2F')]


@pytest.mark.asyncio
async def test_ordinary_connect_has_no_manufactured_path():
    seen = []

    async def app(conn, receive, send):
        seen.append((conn.type, conn.method, conn.path, conn.raw_path, conn.query_string))
        await send({'type': 'http.response.start', 'status': 200, 'headers': []})
        await send({'type': 'http.response.body', 'body': b''})

    actor, _ = _make_h2_actor(app=app)
    actor.receive = AsyncMock(side_effect=[_make_normal_connect_frame(), None])
    await actor.run()
    assert seen == [('http', 'CONNECT', '', b'', b'')]


@pytest.mark.asyncio
@pytest.mark.parametrize('force_asgi', [False, True])
async def test_extended_connect_delivers_path_components(monkeypatch, force_asgi):
    monkeypatch.setenv('BB_H2_ENABLE_WEBSOCKET', '1')
    monkeypatch.setenv('BB_FORCE_ASGI_SCOPE', str(int(force_asgi)))
    seen = []

    async def app(conn, receive, send):
        scope = conn.as_scope() if isinstance(conn, Connection) else conn
        seen.append((scope['path'], scope['raw_path'], scope['query_string']))
        await receive()
        await send({'type': 'websocket.accept'})
        await receive()

    actor, _, _ = _make_ws_actor(app)
    actor.receive = AsyncMock(side_effect=[
        _client_settings(),
        _make_extended_connect_frame(path='//group/a%252Fb;v=1?x=%2F'), None])
    await actor.run()
    assert seen == [('//group/a%2Fb;v=1', b'//group/a%252Fb;v=1', b'x=%2F')]
