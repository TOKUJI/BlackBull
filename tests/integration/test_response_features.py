"""Integration tests for Response variations — content-type, headers, redirects, cookies."""
from http import HTTPStatus

import pytest

from blackbull import BlackBull, JSONResponse
from blackbull.response import Response, cookie_header
from blackbull.testing import TestClient


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
def client():
    with TestClient(_make_app()) as c:
        yield c


@pytest.mark.integration
def test_plain_text_response(client):
    r = client.get('/text')
    assert r.status_code == 200
    assert 'text/plain' in r.headers['content-type']
    assert r.text == 'hello'


@pytest.mark.integration
def test_custom_content_type(client):
    r = client.get('/xml')
    assert r.status_code == 200
    assert 'application/xml' in r.headers['content-type']
    assert r.content == b'<root/>'


@pytest.mark.integration
def test_redirect(client):
    r = client.get('/redirect')  # follow_redirects defaults to False
    assert r.status_code == 302
    assert r.headers['location'] == '/text'


@pytest.mark.integration
def test_custom_response_header(client):
    r = client.get('/custom-header')
    assert r.status_code == 200
    assert r.headers.get('x-request-id') == 'abc123'


@pytest.mark.integration
def test_set_cookie_httponly_and_samesite(client):
    r = client.get('/set-cookie')
    assert r.status_code == 200
    cookie = r.headers.get('set-cookie', '')
    assert 'session=tok123' in cookie
    assert 'HttpOnly' in cookie
    assert 'SameSite=Lax' in cookie


@pytest.mark.integration
def test_set_cookie_no_httponly(client):
    r = client.get('/set-cookie-no-httponly')
    cookie = r.headers.get('set-cookie', '')
    assert 'pref=dark' in cookie
    assert 'HttpOnly' not in cookie
