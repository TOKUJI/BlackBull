"""Integration tests for cookie_header() and parse_cookies()."""
import pytest

from blackbull import BlackBull, JSONResponse
from blackbull.response import cookie_header
from blackbull.request import parse_cookies
from blackbull.testing import TestClient


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
        return parse_cookies(scope)

    return app


@pytest.fixture(scope="module")
def client():
    with TestClient(_make_app()) as c:
        yield c


@pytest.mark.integration
def test_set_cookie_present_in_response(client):
    r = client.get('/set-cookie')
    assert r.status_code == 200
    assert 'set-cookie' in r.headers


@pytest.mark.integration
def test_set_cookie_httponly_and_samesite(client):
    r = client.get('/set-cookie')
    cookie = r.headers.get('set-cookie', '')
    assert 'session=abc123' in cookie
    assert 'HttpOnly' in cookie
    assert 'SameSite=Lax' in cookie


@pytest.mark.integration
def test_set_cookie_no_httponly(client):
    r = client.get('/set-cookie-no-httponly')
    cookie = r.headers.get('set-cookie', '')
    assert 'pref=dark' in cookie
    assert 'HttpOnly' not in cookie


@pytest.mark.integration
def test_multi_cookie_parsed(client):
    r = client.get('/read-cookies', headers={'Cookie': 'a=1; b=hello'})
    assert r.status_code == 200
    data = r.json()
    assert data.get('a') == '1'
    assert data.get('b') == 'hello'


@pytest.mark.integration
def test_cookie_roundtrip(client):
    """Browser flow: server sets cookie, client sends it back, handler reads it."""
    r1 = client.get('/set-cookie')
    set_cookie = r1.headers.get('set-cookie', '')
    name_value = set_cookie.split(';')[0].strip()

    r2 = client.get('/read-cookies', headers={'Cookie': name_value})
    assert r2.status_code == 200
    assert r2.json().get('session') == 'abc123'
