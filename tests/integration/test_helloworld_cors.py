"""Integration tests for the helloworld-cors example.

Re-creates the helloworld-cors app inline and exercises the CORS
middleware end-to-end through the full app stack.
"""
import json
from http import HTTPMethod

import pytest

from blackbull import CORS, BlackBull, JSONResponse
from blackbull.request import read_body
from blackbull.testing import TestClient


def _make_app(allow_origins) -> BlackBull:
    app = BlackBull()

    @app.route(path='/api/hello')
    async def hello():
        return {'greeting': 'Hello, world!', 'cors': 'enabled'}

    @app.route(path='/api/echo', methods=[HTTPMethod.POST])
    async def echo(scope, receive, send):
        body = await read_body(receive)
        data = json.loads(body)
        await send(JSONResponse({'echo': data}))

    app.use(CORS(
        allow_origins=allow_origins,
        allow_methods=['GET', 'POST', 'OPTIONS'],
        allow_headers=['Content-Type', 'Accept', 'X-Custom-Header'],
        max_age=3600,
    ))
    return app


@pytest.fixture(scope="module")
def cors_client():
    """Explicit-origin CORS config."""
    with TestClient(_make_app(allow_origins=['https://example.com'])) as c:
        yield c


@pytest.fixture(scope="module")
def cors_wildcard_client():
    """Wildcard-origin CORS config."""
    with TestClient(_make_app(allow_origins=['*'])) as c:
        yield c


# ---------------------------------------------------------------------------
# No Origin header — CORS middleware passes through, no CORS headers added
# ---------------------------------------------------------------------------

def test_no_origin_no_cors_headers(cors_client):
    r = cors_client.get('/api/hello')
    assert r.status_code == 200
    assert 'access-control-allow-origin' not in r.headers


def test_no_origin_body_still_returned(cors_client):
    r = cors_client.get('/api/hello')
    assert r.json()['greeting'] == 'Hello, world!'


# ---------------------------------------------------------------------------
# Allowed origin — CORS headers present in response
# ---------------------------------------------------------------------------

def test_allowed_origin_acao_header(cors_client):
    r = cors_client.get('/api/hello', headers={'Origin': 'https://example.com'})
    assert r.status_code == 200
    assert r.headers['access-control-allow-origin'] == 'https://example.com'


def test_allowed_origin_vary_origin(cors_client):
    r = cors_client.get('/api/hello', headers={'Origin': 'https://example.com'})
    assert r.headers.get('vary', '').lower() == 'origin'


# ---------------------------------------------------------------------------
# Disallowed origin — response goes through, but without CORS headers
# ---------------------------------------------------------------------------

def test_disallowed_origin_no_cors_headers(cors_client):
    r = cors_client.get('/api/hello', headers={'Origin': 'https://evil.com'})
    assert r.status_code == 200
    assert 'access-control-allow-origin' not in r.headers


# ---------------------------------------------------------------------------
# Wildcard origin
# ---------------------------------------------------------------------------

def test_wildcard_origin_returns_star(cors_wildcard_client):
    r = cors_wildcard_client.get('/api/hello', headers={'Origin': 'https://any-origin.com'})
    assert r.headers['access-control-allow-origin'] == '*'
    assert 'vary' not in r.headers


# ---------------------------------------------------------------------------
# Preflight OPTIONS
# ---------------------------------------------------------------------------

def test_preflight_returns_200(cors_client):
    r = cors_client.options('/api/hello', headers={
        'Origin': 'https://example.com',
        'Access-Control-Request-Method': 'GET',
    })
    assert r.status_code == 200


def test_preflight_cors_headers_present(cors_client):
    r = cors_client.options('/api/hello', headers={
        'Origin': 'https://example.com',
        'Access-Control-Request-Method': 'POST',
    })
    assert r.headers['access-control-allow-origin'] == 'https://example.com'
    assert 'GET' in r.headers['access-control-allow-methods']
    assert 'POST' in r.headers['access-control-allow-methods']
    assert r.headers['access-control-max-age'] == '3600'


def test_preflight_body_is_empty(cors_client):
    r = cors_client.options('/api/hello', headers={
        'Origin': 'https://example.com',
        'Access-Control-Request-Method': 'GET',
    })
    assert r.content == b''


# ---------------------------------------------------------------------------
# POST /api/echo with CORS
# ---------------------------------------------------------------------------

def test_post_echo_with_cors(cors_client):
    payload = {'message': 'hello'}
    r = cors_client.post('/api/echo', json=payload, headers={'Origin': 'https://example.com'})
    assert r.status_code == 200
    assert r.json()['echo'] == payload
    assert r.headers['access-control-allow-origin'] == 'https://example.com'
