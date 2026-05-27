"""Integration tests for request body reading — read_body() over real TCP."""
import asyncio
import json
from http import HTTPMethod
from multiprocessing import Process
from urllib.parse import parse_qs

import httpx
import pytest

from blackbull import BlackBull, JSONResponse
from blackbull.request import read_body
from .conftest import live_server


def _make_app() -> BlackBull:
    app = BlackBull()

    @app.route(path='/echo', methods=[HTTPMethod.POST])
    async def echo(scope, receive, send):
        body = await read_body(receive)
        await send(JSONResponse({'body': body.decode(), 'length': len(body)}))

    @app.route(path='/json-echo', methods=[HTTPMethod.POST])
    async def json_echo(scope, receive, send):
        body = await read_body(receive)
        data = json.loads(body)
        await send(JSONResponse(data))

    @app.route(path='/form', methods=[HTTPMethod.POST])
    async def form(scope, receive, send):
        body = await read_body(receive)
        fields = {k: v[0] for k, v in parse_qs(body.decode()).items()}
        await send(JSONResponse(fields))

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
async def test_small_post_body(live):
    async with httpx.AsyncClient() as c:
        r = await c.post(f'{_base(live)}/echo', content=b'hello world')
    assert r.status_code == 200
    assert r.json()['body'] == 'hello world'
    assert r.json()['length'] == 11


@pytest.mark.integration
@pytest.mark.asyncio
async def test_large_post_body(live):
    payload = b'x' * (1024 * 1024)  # 1 MB
    async with httpx.AsyncClient() as c:
        r = await c.post(f'{_base(live)}/echo', content=payload)
    assert r.status_code == 200
    assert r.json()['length'] == len(payload)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_json_post_roundtrip(live):
    data = {'key': 'value', 'number': 42}
    async with httpx.AsyncClient() as c:
        r = await c.post(f'{_base(live)}/json-echo', json=data)
    assert r.status_code == 200
    assert r.json() == data


@pytest.mark.integration
@pytest.mark.asyncio
async def test_empty_body(live):
    async with httpx.AsyncClient() as c:
        r = await c.post(f'{_base(live)}/echo', content=b'')
    assert r.status_code == 200
    assert r.json()['length'] == 0


@pytest.mark.integration
@pytest.mark.asyncio
async def test_form_urlencoded(live):
    async with httpx.AsyncClient() as c:
        r = await c.post(
            f'{_base(live)}/form',
            content=b'name=alice&role=admin',
            headers={'content-type': 'application/x-www-form-urlencoded'},
        )
    assert r.status_code == 200
    assert r.json() == {'name': 'alice', 'role': 'admin'}
