"""Integration tests for lifespan event hooks (guide.md §9).

Startup hooks fire before the first request; shutdown hooks fire after
graceful stop. Both are invisible to unit tests because they require a
running server process with a live asyncio event loop.
"""
import asyncio
import time
from multiprocessing import Process, Value

import httpx
import pytest

from blackbull import BlackBull
from blackbull.server.server import LifespanManager


def _make_startup_app(startup_counter) -> BlackBull:
    app = BlackBull()

    @app.on_startup
    async def on_start():
        startup_counter.value += 1

    @app.route(path='/startup-count')
    async def startup_count():
        return {'count': startup_counter.value}

    return app


def _run_lifespan_only(app, ready_flag, stop_flag, shutdown_flag):
    """Run startup→idle→shutdown in an isolated event loop with no HTTP server.

    This exercises the full ASGI lifespan protocol without the complexity of
    stopping a live TCP server gracefully.
    """
    async def _go():
        async with LifespanManager(app):
            ready_flag.value = 1
            while not stop_flag.value:
                await asyncio.sleep(0.05)
        # LifespanManager.__aexit__ has now fired the shutdown handshake and
        # the on_shutdown hook has updated shutdown_flag before we get here.

    asyncio.run(_go())


@pytest.fixture(scope="module")
def live_startup():
    counter = Value('i', 0)
    app = _make_startup_app(counter)
    app.create_server(port=0)
    p = Process(target=lambda: asyncio.run(app.run()))
    p.start()
    app.wait_for_port(timeout=10.0)
    yield app
    app.stop()
    p.terminate()
    p.join(timeout=5)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_startup_hook_runs_before_first_request(live_startup):
    async with httpx.AsyncClient() as c:
        r = await c.get(f'http://127.0.0.1:{live_startup.port}/startup-count')
    assert r.status_code == 200
    # startup ran exactly once before the server became ready
    assert r.json()['count'] == 1


@pytest.mark.integration
def test_shutdown_hook_runs_on_graceful_stop():
    ready_flag    = Value('i', 0)
    stop_flag     = Value('i', 0)
    shutdown_flag = Value('i', 0)

    app = BlackBull()

    @app.on_shutdown
    async def on_stop():
        shutdown_flag.value = 1

    p = Process(target=_run_lifespan_only,
                args=(app, ready_flag, stop_flag, shutdown_flag))
    p.start()

    # wait for the lifespan task to finish startup
    deadline = time.monotonic() + 10.0
    while not ready_flag.value and time.monotonic() < deadline:
        time.sleep(0.05)
    assert ready_flag.value, 'lifespan startup did not complete in time'

    # signal the child to begin graceful shutdown
    stop_flag.value = 1
    p.join(timeout=10)

    assert shutdown_flag.value == 1, 'shutdown hook did not run'
