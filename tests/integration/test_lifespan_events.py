"""Integration tests for lifespan event hooks (guide.md §9).

Startup hooks fire before the first request via the ASGI ``lifespan``
protocol; shutdown hooks fire on the matching ``lifespan.shutdown``
event.  The first test drives both ends through :class:`TestClient`,
which speaks the lifespan protocol natively.  The second test
exercises :class:`LifespanManager` directly via a child process — it's
verifying server-side wiring, not app behaviour, so it stays
out-of-process.
"""
import asyncio
import time
from multiprocessing import Process, Value

import pytest

from blackbull import BlackBull
from blackbull.server.server import LifespanManager
from blackbull.testing import TestClient


def _run_lifespan_only(app, ready_flag, stop_flag, shutdown_flag):
    """Run startup→idle→shutdown in an isolated event loop with no HTTP server.

    Exercises the full ASGI lifespan protocol without binding sockets.
    ``shutdown_flag`` is written by ``app``'s ``on_shutdown`` handler.
    """
    async def _go():
        async with LifespanManager(app):
            ready_flag.value = 1
            while not stop_flag.value:
                await asyncio.sleep(0.05)
        # LifespanManager.__aexit__ has now fired the shutdown handshake
        # and the on_shutdown hook has updated shutdown_flag before we
        # get here.

    asyncio.run(_go())


@pytest.mark.integration
def test_startup_hook_runs_before_first_request():
    counter = {'value': 0}
    app = BlackBull()

    @app.on_startup
    async def on_start():
        counter['value'] += 1

    @app.route(path='/startup-count')
    async def startup_count():
        return {'count': counter['value']}

    with TestClient(app) as client:
        r = client.get('/startup-count')
    assert r.status_code == 200
    # Startup ran exactly once before the first request.
    assert r.json()['count'] == 1


@pytest.mark.integration
def test_shutdown_hook_runs_on_graceful_stop():
    """Verifies the server's :class:`LifespanManager` fires the shutdown
    handshake on graceful exit.  Kept out-of-process so the
    server-side lifespan flow is exercised end-to-end."""
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

    deadline = time.monotonic() + 10.0
    while not ready_flag.value and time.monotonic() < deadline:
        time.sleep(0.05)
    assert ready_flag.value, 'lifespan startup did not complete in time'

    stop_flag.value = 1
    p.join(timeout=10)

    assert shutdown_flag.value == 1, 'shutdown hook did not run'
