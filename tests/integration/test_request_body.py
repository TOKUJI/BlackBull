"""Integration tests for request body reading — read_body() through the full app stack."""
import json
from http import HTTPMethod
from urllib.parse import parse_qs

import pytest

from blackbull import BlackBull, JSONResponse
from blackbull.request import read_body
from blackbull.testing import TestClient


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
def client():
    with TestClient(_make_app()) as c:
        yield c


@pytest.mark.integration
def test_small_post_body(client):
    r = client.post('/echo', content=b'hello world')
    assert r.status_code == 200
    assert r.json()['body'] == 'hello world'
    assert r.json()['length'] == 11


@pytest.mark.integration
def test_large_post_body(client):
    payload = b'x' * (1024 * 1024)  # 1 MB
    r = client.post('/echo', content=payload)
    assert r.status_code == 200
    assert r.json()['length'] == len(payload)


@pytest.mark.integration
def test_json_post_roundtrip(client):
    data = {'key': 'value', 'number': 42}
    r = client.post('/json-echo', json=data)
    assert r.status_code == 200
    assert r.json() == data


@pytest.mark.integration
def test_empty_body(client):
    r = client.post('/echo', content=b'')
    assert r.status_code == 200
    assert r.json()['length'] == 0


@pytest.mark.integration
def test_form_urlencoded(client):
    r = client.post(
        '/form',
        content=b'name=alice&role=admin',
        headers={'content-type': 'application/x-www-form-urlencoded'},
    )
    assert r.status_code == 200
    assert r.json() == {'name': 'alice', 'role': 'admin'}
