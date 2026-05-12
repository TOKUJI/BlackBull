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


@pytest.mark.integration
@pytest.mark.asyncio
async def test_range_request_206(live):
    async with httpx.AsyncClient() as c:
        r = await c.get(
            f'{_base(live)}/static/hello.txt',
            headers={'Range': 'bytes=0-4'},
        )
    assert r.status_code == 206
    assert r.content == b'Hello'
    assert 'content-range' in r.headers


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
