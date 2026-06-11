"""Integration tests for HTTP method routing and 405 Method Not Allowed."""
import json
from http import HTTPMethod

import pytest

from blackbull import BlackBull, JSONResponse
from blackbull.request import read_body
from blackbull.testing import TestClient


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
def client():
    with TestClient(_make_app()) as c:
        yield c


@pytest.mark.integration
def test_put(client):
    payload = {'key': 'value'}
    r = client.put('/resource', json=payload)
    assert r.status_code == 200
    assert r.json() == payload


@pytest.mark.integration
def test_patch(client):
    payload = {'field': 'updated'}
    r = client.patch('/resource', json=payload)
    assert r.status_code == 200
    assert r.json() == payload


@pytest.mark.integration
def test_delete(client):
    r = client.delete('/resource/7')
    assert r.status_code == 200
    assert r.json() == {'deleted': 7}


@pytest.mark.integration
def test_head_returns_200_no_body(client):
    r = client.head('/resource')
    assert r.status_code == 200
    assert r.content == b''


@pytest.mark.integration
def test_wrong_method_405(client):
    r = client.post('/resource/1')
    assert r.status_code == 405
