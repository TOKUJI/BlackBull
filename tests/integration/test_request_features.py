"""Integration tests for request header access, cookie parsing, and query strings."""
import pytest

from blackbull import BlackBull
from blackbull.request import parse_cookies
from blackbull.testing import TestClient


def _make_app() -> BlackBull:
    app = BlackBull()

    @app.route(path='/header-echo')
    async def header_echo(scope):
        value = scope['headers'].get(b'x-custom-header')
        return {'value': value.decode() if value else None}

    @app.route(path='/header-multivalue')
    async def header_multivalue(scope):
        pairs = scope['headers'].getlist(b'accept')
        return {'values': [v.decode() for _, v in pairs]}

    @app.route(path='/cookies')
    async def cookies(scope):
        return parse_cookies(scope)

    @app.route(path='/query')
    async def query(scope):
        return {'query_string': scope.get('query_string', b'').decode()}

    return app


@pytest.fixture(scope="module")
def client():
    with TestClient(_make_app()) as c:
        yield c


@pytest.mark.integration
def test_case_insensitive_header(client):
    r = client.get('/header-echo', headers={'X-Custom-Header': 'hello'})
    assert r.status_code == 200
    assert r.json()['value'] == 'hello'


@pytest.mark.integration
def test_multi_value_header_getlist(client):
    r = client.get('/header-multivalue', headers={'Accept': 'text/html, application/json'})
    assert r.status_code == 200
    assert len(r.json()['values']) >= 1


@pytest.mark.integration
def test_cookie_parsed(client):
    r = client.get('/cookies', headers={'Cookie': 'a=1; b=2'})
    assert r.status_code == 200
    data = r.json()
    assert data.get('a') == '1'
    assert data.get('b') == '2'


@pytest.mark.integration
def test_query_string(client):
    r = client.get('/query', params={'q': 'hello', 'page': '2'})
    assert r.status_code == 200
    qs = r.json()['query_string']
    assert 'q=hello' in qs
    assert 'page=2' in qs
