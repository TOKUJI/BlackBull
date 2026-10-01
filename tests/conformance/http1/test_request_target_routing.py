"""Request-target components survive parsing, routing, and ASGI conversion."""
import pytest

from blackbull import BlackBull
from blackbull.env import reset_settings_cache
from blackbull.utils import Scheme

from .test_dual_path_identity import _drive


@pytest.mark.asyncio
@pytest.mark.parametrize('forced_scope', [False, True])
@pytest.mark.parametrize('target,path,raw_path,query,host,server', [
    (b'//group/review?q=ok', '//group/review', b'//group/review',
     b'q=ok', b'original.invalid:8000', ('original.invalid', 8000)),
    (b'/a%252Fb?x=%2F', '/a%2Fb', b'/a%252Fb',
     b'x=%2F', b'original.invalid:8000', ('original.invalid', 8000)),
    (b'http://review.invalid?q=ok', '/', b'/',
     b'q=ok', b'review.invalid', ('review.invalid', 80)),
    (b'http://review.invalid?next=/review', '/', b'/',
     b'next=/review', b'review.invalid', ('review.invalid', 80)),
    (b'http://[::1]:8080?next=//elsewhere/path', '/', b'/',
     b'next=//elsewhere/path', b'[::1]:8080', ('::1', 8080)),
])
async def test_target_selects_the_same_route_in_native_and_scope_lanes(
        monkeypatch, forced_scope, target, path, raw_path, query, host, server):
    monkeypatch.setenv('BB_FORCE_ASGI_SCOPE', str(int(forced_scope)))
    reset_settings_cache()
    app = BlackBull()
    seen = []

    @app.route(path=path)
    async def correct(conn, receive, send):
        scope = conn.as_scope()
        seen.append((conn.path, conn.raw_path, conn.query_string,
                     conn.headers.get(b'host'), conn.server,
                     scope['path'], scope['raw_path'], scope['query_string'],
                     scope['server']))
        await send(b'correct route', 200)

    async def wrong(conn, receive, send):
        await send(b'wrong route', 200)

    for other_path in ('/review', '/a/b'):
        app.route(path=other_path)(wrong)

    response = await _drive(
        app, b'GET ' + target + b' HTTP/1.1\r\n'
        b'Host: original.invalid:8000\r\nConnection: close\r\n\r\n')

    assert response.startswith(b'HTTP/1.1 200 '), response
    assert response.endswith(b'correct route'), response
    assert seen == [(path, raw_path, query, host, server,
                     path, raw_path, query, list(server))]


@pytest.mark.asyncio
@pytest.mark.parametrize('forced_scope', [False, True])
async def test_websocket_upgrade_delivers_preserved_target(monkeypatch, forced_scope):
    monkeypatch.setenv('BB_FORCE_ASGI_SCOPE', str(int(forced_scope)))
    reset_settings_cache()
    app = BlackBull()
    seen = []

    @app.route(path='//group/ws;v/a%2Fb', scheme=Scheme.websocket)
    async def websocket(conn, receive, send):
        scope = conn.as_scope()
        seen.append((conn.type, conn.path, conn.raw_path, conn.query_string,
                     conn.server, scope['path'], scope['raw_path'],
                     scope['query_string'], scope['server']))
        assert (await receive())['type'] == 'websocket.connect'
        await send({'type': 'websocket.accept'})
        await send({'type': 'websocket.close'})

    response = await _drive(
        app, b'GET //group/ws;v/a%252Fb?next=/x%2Fy HTTP/1.1\r\n'
        b'Host: original.invalid:8000\r\nUpgrade: websocket\r\n'
        b'Connection: Upgrade\r\nSec-WebSocket-Version: 13\r\n'
        b'Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n\r\n')

    assert response.startswith(b'HTTP/1.1 101 '), response
    assert seen == [('websocket', '//group/ws;v/a%2Fb', b'//group/ws;v/a%252Fb',
                     b'next=/x%2Fy', ('original.invalid', 8000),
                     '//group/ws;v/a%2Fb', b'//group/ws;v/a%252Fb',
                     b'next=/x%2Fy', ['original.invalid', 8000])]
