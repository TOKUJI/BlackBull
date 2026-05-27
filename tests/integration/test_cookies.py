"""Integration tests for cookie_header() and parse_cookies() over HTTP."""
import asyncio
from multiprocessing import Process

import httpx
import pytest

from blackbull import BlackBull, JSONResponse
from blackbull.response import cookie_header
from blackbull.request import parse_cookies
from .conftest import live_server


def _make_app() -> BlackBull:
    app = BlackBull()

    @app.route(path='/set-cookie')
    async def set_cookie_route(scope, receive, send):
        await send(JSONResponse(
            {'ok': True},
            headers=[cookie_header('session', 'abc123')],
        ))

    @app.route(path='/set-cookie-no-httponly')
    async def set_cookie_no_httponly(scope, receive, send):
        await send(JSONResponse(
            {'ok': True},
            headers=[cookie_header('pref', 'dark', http_only=False)],
        ))

    @app.route(path='/read-cookies')
    async def read_cookies(scope):
        jar = parse_cookies(scope)
        return jar

    return app


@pytest.fixture(scope="module")
def live():
    app = _make_app()
    with live_server(app) as handle:
        yield handle
def _base(app) -> str:
    return f'http://127.0.0.1:{app.port}'


@pytest.mark.integration
@pytest.mark.asyncio
async def test_set_cookie_present_in_response(live):
    async with httpx.AsyncClient() as c:
        r = await c.get(f'{_base(live)}/set-cookie')
    assert r.status_code == 200
    assert 'set-cookie' in r.headers


@pytest.mark.integration
@pytest.mark.asyncio
async def test_set_cookie_httponly_and_samesite(live):
    async with httpx.AsyncClient() as c:
        r = await c.get(f'{_base(live)}/set-cookie')
    cookie = r.headers.get('set-cookie', '')
    assert 'session=abc123' in cookie
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


@pytest.mark.integration
@pytest.mark.asyncio
async def test_multi_cookie_parsed(live):
    async with httpx.AsyncClient() as c:
        r = await c.get(
            f'{_base(live)}/read-cookies',
            headers={'Cookie': 'a=1; b=hello'},
        )
    assert r.status_code == 200
    data = r.json()
    assert data.get('a') == '1'
    assert data.get('b') == 'hello'


@pytest.mark.integration
@pytest.mark.asyncio
async def test_cookie_roundtrip(live):
    """Browser flow: server sets cookie, client sends it back, handler reads it."""
    async with httpx.AsyncClient() as c:
        # Get the Set-Cookie
        r1 = await c.get(f'{_base(live)}/set-cookie')
        set_cookie = r1.headers.get('set-cookie', '')
        # Extract name=value (before the first ';')
        name_value = set_cookie.split(';')[0].strip()

        # Send it back as a Cookie header
        r2 = await c.get(
            f'{_base(live)}/read-cookies',
            headers={'Cookie': name_value},
        )
    assert r2.status_code == 200
    assert r2.json().get('session') == 'abc123'
