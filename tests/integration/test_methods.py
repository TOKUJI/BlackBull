"""Integration tests for HTTP method routing and 405 Method Not Allowed."""
import asyncio
import json
from http import HTTPMethod
from multiprocessing import Process

import httpx
import pytest

from blackbull import BlackBull, JSONResponse
from blackbull.request import read_body


def _make_app() -> BlackBull:
    app = BlackBull()

    @app.route(path='/resource', methods=[HTTPMethod.GET])
    async def get_resource():
        return {'method': 'GET'}

    @app.route(path='/resource', methods=[HTTPMethod.PUT])
    async def put_resource(scope, receive, send):
        body = await read_body(receive)
        await send(JSONResponse(json.loads(body)))

    @app.route(path='/resource', methods=[HTTPMethod.PATCH])
    async def patch_resource(scope, receive, send):
        body = await read_body(receive)
        await send(JSONResponse(json.loads(body)))

    @app.route(path='/resource/{id:int}', methods=[HTTPMethod.DELETE])
    async def delete_resource(id: int):
        return {'deleted': id}

    @app.route(path='/resource', methods=[HTTPMethod.HEAD])
    async def head_resource(scope, receive, send):
        await send({'type': 'http.response.start', 'status': 200,
                    'headers': [(b'x-size', b'0')]})
        await send({'type': 'http.response.body', 'body': b'', 'more_body': False})

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
async def test_put(live):
    payload = {'key': 'value'}
    async with httpx.AsyncClient() as c:
        r = await c.put(f'{_base(live)}/resource', json=payload)
    assert r.status_code == 200
    assert r.json() == payload


@pytest.mark.integration
@pytest.mark.asyncio
async def test_patch(live):
    payload = {'field': 'updated'}
    async with httpx.AsyncClient() as c:
        r = await c.patch(f'{_base(live)}/resource', json=payload)
    assert r.status_code == 200
    assert r.json() == payload


@pytest.mark.integration
@pytest.mark.asyncio
async def test_delete(live):
    async with httpx.AsyncClient() as c:
        r = await c.delete(f'{_base(live)}/resource/7')
    assert r.status_code == 200
    assert r.json() == {'deleted': 7}


@pytest.mark.integration
@pytest.mark.asyncio
async def test_head_returns_200_no_body(live):
    async with httpx.AsyncClient() as c:
        r = await c.head(f'{_base(live)}/resource')
    assert r.status_code == 200
    assert r.content == b''


@pytest.mark.integration
@pytest.mark.asyncio
async def test_wrong_method_405(live):
    async with httpx.AsyncClient() as c:
        r = await c.post(f'{_base(live)}/resource/1')
    assert r.status_code == 405
