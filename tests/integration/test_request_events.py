"""Integration tests for per-request event hooks (guide.md §9).

``request_received`` and ``request_completed`` are fired by the
server-side :class:`EventAggregator` in
[`blackbull/server/http1_actor.py`](../../blackbull/server/http1_actor.py)
— they're outside the path taken by ``BlackBull.__call__`` alone, so
``TestClient`` can't see them.  These tests therefore stay
socket-bound (forked worker + ephemeral TCP port) to exercise the
real server-side aggregator wiring.

The app-side hooks ``before_handler`` and ``after_handler`` *are*
dispatched from inside ``BlackBull._dispatch`` and so can be — and
are — tested through ``TestClient`` directly elsewhere (e.g. via the
unit tests in ``tests/unit/``).
"""
import asyncio
import time
from multiprocessing import Process

import httpx
import pytest

from blackbull import BlackBull
from .conftest import live_server


def _make_app() -> BlackBull:
    app = BlackBull()

    _state = {
        'received':  0,
        'before':    0,
        'after':     0,
        'completed': 0,
        'injected':  False,
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
    with live_server(app) as handle:
        yield handle


def _base(app) -> str:
    return f'http://127.0.0.1:{app.port}'


def _get_state(base: str) -> dict:
    """Synchronously read the server-side counter state.

    Polls briefly to allow fire-and-forget observer tasks to complete.
    """
    deadline = time.monotonic() + 2.0
    last = {}
    while time.monotonic() < deadline:
        r = httpx.get(f'{base}/state')
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
