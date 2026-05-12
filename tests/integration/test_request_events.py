"""Integration tests for per-request event hooks (guide.md §9).

request_received, before_handler, after_handler, and request_completed
all fire during normal request processing.  Each test drives the counter
via HTTP and reads the result back through a /state endpoint in the same
server process.
"""
import asyncio
import time
from multiprocessing import Process

import httpx
import pytest

from blackbull import BlackBull


def _make_app() -> BlackBull:
    app = BlackBull()

    _state = {
        'received': 0,
        'before':   0,
        'after':    0,
        'completed': 0,
        'injected': False,
    }

    @app.on('request_received')
    async def on_received(event):
        if event.detail.get('path') != '/state':
            _state['received'] += 1

    @app.on('before_handler')
    async def on_before(event):
        if event.detail.get('path') != '/state':
            _state['before'] += 1

    @app.on('after_handler')
    async def on_after(event):
        if event.detail.get('path') != '/state':
            _state['after'] += 1

    @app.on('request_completed')
    async def on_completed(event):
        if event.detail.get('path') != '/state':
            _state['completed'] += 1

    @app.route(path='/trigger')
    async def trigger():
        return {'ok': True}

    @app.route(path='/state')
    async def get_state():
        return dict(_state)

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


def _get_state(base: str) -> dict:
    """Synchronously read the server-side counter state.

    Polls briefly to allow fire-and-forget observer tasks to complete.
    """
    import httpx as _httpx
    deadline = time.monotonic() + 2.0
    last = {}
    while time.monotonic() < deadline:
        r = _httpx.get(f'{base}/state')
        last = r.json()
        if last.get('completed', 0) >= 1:
            return last
        time.sleep(0.05)
    return last


@pytest.mark.integration
@pytest.mark.asyncio
async def test_request_received_fires(live):
    base = _base(live)
    async with httpx.AsyncClient() as c:
        await c.get(f'{base}/trigger')
    state = _get_state(base)
    assert state['received'] >= 1


@pytest.mark.integration
@pytest.mark.asyncio
async def test_before_handler_fires(live):
    base = _base(live)
    async with httpx.AsyncClient() as c:
        await c.get(f'{base}/trigger')
    state = _get_state(base)
    assert state['before'] >= 1


@pytest.mark.integration
@pytest.mark.asyncio
async def test_after_handler_fires(live):
    base = _base(live)
    async with httpx.AsyncClient() as c:
        await c.get(f'{base}/trigger')
    state = _get_state(base)
    assert state['after'] >= 1


@pytest.mark.integration
@pytest.mark.asyncio
async def test_request_completed_fires(live):
    base = _base(live)
    async with httpx.AsyncClient() as c:
        await c.get(f'{base}/trigger')
    state = _get_state(base)
    assert state['completed'] >= 1
