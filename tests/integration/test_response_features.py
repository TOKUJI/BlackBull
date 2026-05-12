"""Integration tests for Response variations — content-type, headers, redirects, cookies."""
import asyncio
from http import HTTPStatus
from multiprocessing import Process

import httpx
import pytest

from blackbull import BlackBull, JSONResponse
from blackbull.response import Response, cookie_header


def _make_app() -> BlackBull:
    app = BlackBull()

    @app.route(path='/text')
    async def text(scope, receive, send):
        await send(Response('hello', content_type='text/plain; charset=utf-8'))

    @app.route(path='/xml')
    async def xml(scope, receive, send):
        await send(Response(b'<root/>', content_type='application/xml'))

    @app.route(path='/redirect')
    async def redirect(scope, receive, send):
        await send(Response(
            b'',
            status=HTTPStatus.FOUND,
            headers=[(b'location', b'/text')],
        ))

    @app.route(path='/custom-header')
    async def custom_header(scope, receive, send):
        await send(JSONResponse({'ok': True}, headers=[(b'x-request-id', b'abc123')]))

    @app.route(path='/set-cookie')
    async def set_cookie(scope, receive, send):
        await send(JSONResponse({'ok': True}, headers=[cookie_header('session', 'tok123')]))

    @app.route(path='/set-cookie-no-httponly')
    async def set_cookie_no_httponly(scope, receive, send):
        await send(JSONResponse(
            {'ok': True},
            headers=[cookie_header('pref', 'dark', http_only=False)],
        ))

    return app


@pytest.fixture(scope="module")
def live():
    app = _make_app()
    app.create_server(port=0)
    p = Process(target=lambda: asyncio.run(app.run()))
    p.start()
    app.wait_for_port(timeout=10.0)
    yield app
    app.stop()
    p.terminate()
    p.join(timeout=5)


def _base(app) -> str:
    return f'http://127.0.0.1:{app.port}'


@pytest.mark.integration
@pytest.mark.asyncio
async def test_plain_text_response(live):
    async with httpx.AsyncClient() as c:
        r = await c.get(f'{_base(live)}/text')
    assert r.status_code == 200
    assert 'text/plain' in r.headers['content-type']
    assert r.text == 'hello'


@pytest.mark.integration
@pytest.mark.asyncio
async def test_custom_content_type(live):
    async with httpx.AsyncClient() as c:
        r = await c.get(f'{_base(live)}/xml')
    assert r.status_code == 200
    assert 'application/xml' in r.headers['content-type']
    assert r.content == b'<root/>'


@pytest.mark.integration
@pytest.mark.asyncio
async def test_redirect(live):
    async with httpx.AsyncClient(follow_redirects=False) as c:
        r = await c.get(f'{_base(live)}/redirect')
    assert r.status_code == 302
    assert r.headers['location'] == '/text'


@pytest.mark.integration
@pytest.mark.asyncio
async def test_custom_response_header(live):
    async with httpx.AsyncClient() as c:
        r = await c.get(f'{_base(live)}/custom-header')
    assert r.status_code == 200
    assert r.headers.get('x-request-id') == 'abc123'


@pytest.mark.integration
@pytest.mark.asyncio
async def test_set_cookie_httponly_and_samesite(live):
    async with httpx.AsyncClient() as c:
        r = await c.get(f'{_base(live)}/set-cookie')
    assert r.status_code == 200
    cookie = r.headers.get('set-cookie', '')
    assert 'session=tok123' in cookie
    assert 'HttpOnly' in cookie
    assert 'SameSite=Lax' in cookie


@pytest.mark.integration
@pytest.mark.asyncio
async def test_set_cookie_no_httponly(live):
    async with httpx.AsyncClient() as c:
        r = await c.get(f'{_base(live)}/set-cookie-no-httponly')
    cookie = r.headers.get('set-cookie', '')
    assert 'pref=dark' in cookie
    assert 'HttpOnly' not in cookie
