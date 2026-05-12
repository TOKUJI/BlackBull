"""Integration tests for helloworld-cors.py.

Starts the app in a child process on a random port (HTTP/1.1, no TLS) and
exercises the CORS middleware end-to-end through real network calls.
"""
import asyncio
import json
from http import HTTPMethod
from multiprocessing import Process

import httpx
import pytest
import pytest_asyncio

from blackbull import CORS, BlackBull, JSONResponse
from blackbull.request import read_body


# ---------------------------------------------------------------------------
# Re-create the helloworld-cors app inline so the test is self-contained.
# Using the same routes and CORS config as the example file.
# ---------------------------------------------------------------------------

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


def _run(app):
    asyncio.run(app.run())


@pytest_asyncio.fixture
async def cors_app():
    """BlackBull app with explicit CORS origin; runs in a child process."""
    app = _make_app(allow_origins=['https://example.com'])
    app.create_server(port=0)

    p = Process(target=_run, args=(app,))
    p.start()
    app.wait_for_port(timeout=10.0)

    yield app

    app.stop()
    p.terminate()
    p.join(timeout=5)


@pytest_asyncio.fixture
async def cors_wildcard_app():
    """BlackBull app with wildcard CORS origin; runs in a child process."""
    app = _make_app(allow_origins=['*'])
    app.create_server(port=0)

    p = Process(target=_run, args=(app,))
    p.start()
    app.wait_for_port(timeout=10.0)

    yield app

    app.stop()
    p.terminate()
    p.join(timeout=5)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _base(app) -> str:
    return f'http://127.0.0.1:{app.port}'


# ---------------------------------------------------------------------------
# No Origin header — CORS middleware passes through, no CORS headers added
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_no_origin_no_cors_headers(cors_app):
    async with httpx.AsyncClient() as c:
        r = await c.get(f'{_base(cors_app)}/api/hello')
    assert r.status_code == 200
    assert 'access-control-allow-origin' not in r.headers


@pytest.mark.asyncio
async def test_no_origin_body_still_returned(cors_app):
    async with httpx.AsyncClient() as c:
        r = await c.get(f'{_base(cors_app)}/api/hello')
    assert r.json()['greeting'] == 'Hello, world!'


# ---------------------------------------------------------------------------
# Allowed origin — CORS headers present in response
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_allowed_origin_acao_header(cors_app):
    async with httpx.AsyncClient() as c:
        r = await c.get(
            f'{_base(cors_app)}/api/hello',
            headers={'Origin': 'https://example.com'},
        )
    assert r.status_code == 200
    assert r.headers['access-control-allow-origin'] == 'https://example.com'


@pytest.mark.asyncio
async def test_allowed_origin_vary_origin(cors_app):
    async with httpx.AsyncClient() as c:
        r = await c.get(
            f'{_base(cors_app)}/api/hello',
            headers={'Origin': 'https://example.com'},
        )
    assert r.headers.get('vary', '').lower() == 'origin'


# ---------------------------------------------------------------------------
# Disallowed origin — response goes through, but without CORS headers
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_disallowed_origin_no_cors_headers(cors_app):
    async with httpx.AsyncClient() as c:
        r = await c.get(
            f'{_base(cors_app)}/api/hello',
            headers={'Origin': 'https://evil.com'},
        )
    assert r.status_code == 200
    assert 'access-control-allow-origin' not in r.headers


# ---------------------------------------------------------------------------
# Wildcard origin
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_wildcard_origin_returns_star(cors_wildcard_app):
    async with httpx.AsyncClient() as c:
        r = await c.get(
            f'{_base(cors_wildcard_app)}/api/hello',
            headers={'Origin': 'https://any-origin.com'},
        )
    assert r.headers['access-control-allow-origin'] == '*'
    assert 'vary' not in r.headers


# ---------------------------------------------------------------------------
# Preflight OPTIONS
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_preflight_returns_200(cors_app):
    async with httpx.AsyncClient() as c:
        r = await c.options(
            f'{_base(cors_app)}/api/hello',
            headers={
                'Origin': 'https://example.com',
                'Access-Control-Request-Method': 'GET',
            },
        )
    assert r.status_code == 200


@pytest.mark.asyncio
async def test_preflight_cors_headers_present(cors_app):
    async with httpx.AsyncClient() as c:
        r = await c.options(
            f'{_base(cors_app)}/api/hello',
            headers={
                'Origin': 'https://example.com',
                'Access-Control-Request-Method': 'POST',
            },
        )
    assert r.headers['access-control-allow-origin'] == 'https://example.com'
    assert 'GET' in r.headers['access-control-allow-methods']
    assert 'POST' in r.headers['access-control-allow-methods']
    assert r.headers['access-control-max-age'] == '3600'


@pytest.mark.asyncio
async def test_preflight_body_is_empty(cors_app):
    async with httpx.AsyncClient() as c:
        r = await c.options(
            f'{_base(cors_app)}/api/hello',
            headers={
                'Origin': 'https://example.com',
                'Access-Control-Request-Method': 'GET',
            },
        )
    assert r.content == b''


# ---------------------------------------------------------------------------
# POST /api/echo with CORS
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_post_echo_with_cors(cors_app):
    payload = {'message': 'hello'}
    async with httpx.AsyncClient() as c:
        r = await c.post(
            f'{_base(cors_app)}/api/echo',
            json=payload,
            headers={'Origin': 'https://example.com'},
        )
    assert r.status_code == 200
    assert r.json()['echo'] == payload
    assert r.headers['access-control-allow-origin'] == 'https://example.com'
