"""Integration tests for request header access, cookie parsing, and query strings."""
import asyncio
from multiprocessing import Process

import httpx
import pytest

from blackbull import BlackBull, JSONResponse
from blackbull.request import parse_cookies


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
        jar = parse_cookies(scope)
        return jar

    @app.route(path='/query')
    async def query(scope):
        qs = scope.get('query_string', b'').decode()
        return {'query_string': qs}

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
async def test_case_insensitive_header(live):
    async with httpx.AsyncClient() as c:
        r = await c.get(
            f'{_base(live)}/header-echo',
            headers={'X-Custom-Header': 'hello'},
        )
    assert r.status_code == 200
    assert r.json()['value'] == 'hello'


@pytest.mark.integration
@pytest.mark.asyncio
async def test_multi_value_header_getlist(live):
    async with httpx.AsyncClient() as c:
        r = await c.get(
            f'{_base(live)}/header-multivalue',
            headers={'Accept': 'text/html, application/json'},
        )
    assert r.status_code == 200
    assert len(r.json()['values']) >= 1


@pytest.mark.integration
@pytest.mark.asyncio
async def test_cookie_parsed(live):
    async with httpx.AsyncClient() as c:
        r = await c.get(
            f'{_base(live)}/cookies',
            headers={'Cookie': 'a=1; b=2'},
        )
    assert r.status_code == 200
    data = r.json()
    assert data.get('a') == '1'
    assert data.get('b') == '2'


@pytest.mark.integration
@pytest.mark.asyncio
async def test_query_string(live):
    async with httpx.AsyncClient() as c:
        r = await c.get(f'{_base(live)}/query', params={'q': 'hello', 'page': '2'})
    assert r.status_code == 200
    qs = r.json()['query_string']
    assert 'q=hello' in qs
    assert 'page=2' in qs
