"""Integration tests for app.static() — static file serving (guide.md §16.2).

Unit tests cover path-traversal logic in isolation; these tests confirm the
full file-serving pipeline over a real TCP connection.
"""
import asyncio
import tempfile
from multiprocessing import Process
from pathlib import Path

import httpx
import pytest

from blackbull import BlackBull

from .conftest import live_server


def _make_app(root_dir: str) -> BlackBull:
    app = BlackBull()
    app.static(url_prefix='/static', root_dir=root_dir)
    return app


@pytest.fixture(scope="module")
def live(tmp_path_factory):
    root = tmp_path_factory.mktemp('static')
    (root / 'hello.txt').write_bytes(b'Hello, world!')
    (root / 'data.json').write_bytes(b'{"key": "value"}')
    (root / 'binary.bin').write_bytes(bytes(range(256)))

    app = _make_app(str(root))
    with live_server(app) as handle:
        yield handle


def _base(app) -> str:
    return f'http://127.0.0.1:{app.port}'


@pytest.mark.integration
@pytest.mark.asyncio
async def test_file_served(live):
    async with httpx.AsyncClient() as c:
        r = await c.get(f'{_base(live)}/static/hello.txt')
    assert r.status_code == 200
    assert r.content == b'Hello, world!'


@pytest.mark.integration
@pytest.mark.asyncio
async def test_missing_file_404(live):
    async with httpx.AsyncClient() as c:
        r = await c.get(f'{_base(live)}/static/does-not-exist.txt')
    assert r.status_code == 404


async def _get_h1(port, path, rng):
    async with httpx.AsyncClient() as c:
        r = await c.get(f'http://127.0.0.1:{port}{path}', headers={'Range': rng})
    return r.status_code, r.headers.get('content-range'), r.content


async def _get_h2(port, path, rng):
    from blackbull.client.http2 import HTTP2Client
    async with HTTP2Client('127.0.0.1', port) as c:
        r = await c.request('GET', path, headers=[('range', rng)])
    cr = r.headers.get(b'content-range')
    return r.status, cr.decode() if cr else None, r.body


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize('get', [_get_h1, _get_h2], ids=['http1', 'http2'])
@pytest.mark.parametrize('rng,status,content_range,body', [
    ('bytes=0-4', 206, 'bytes 0-4/13', b'Hello'),
    ('bytes=7-999', 206, 'bytes 7-12/13', b'world!'),
    ('bytes=5-3', 200, None, b'Hello, world!'),
    ('bytes=99-', 416, 'bytes */13', b''),
])
async def test_range_request(live, get, rng, status, content_range, body):
    assert await get(live.port, '/static/hello.txt', rng) == (
        status, content_range, body)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_path_traversal_blocked(live):
    async with httpx.AsyncClient() as c:
        r = await c.get(f'{_base(live)}/static/../../etc/passwd')
    assert r.status_code in (400, 404)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_url_prefix_mapping(live):
    # Files are under root_dir but served at /static/<name>
    async with httpx.AsyncClient() as c:
        r = await c.get(f'{_base(live)}/static/data.json')
    assert r.status_code == 200
    assert r.headers.get('content-type', '').startswith('application/json')


@pytest.mark.integration
@pytest.mark.asyncio
async def test_content_type_inferred(live):
    async with httpx.AsyncClient() as c:
        r = await c.get(f'{_base(live)}/static/hello.txt')
    assert 'text/plain' in r.headers.get('content-type', '')
