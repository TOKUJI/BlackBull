"""Shared helpers for integration tests.

Sprint 22 collapsed the per-instance ``app.create_server`` / ``app.run`` /
``app.wait_for_port`` / ``app.stop`` API into a single sync ``app.run()``
on :class:`BlackBull`, plus a dedicated :class:`ASGIServer` for embedded
use (pre-binding sockets, mTLS configuration, integration tests).

Every integration test in this directory follows the same pattern:
build an app, bind an ephemeral port, fork a worker, hit the bound port
with an HTTP client, kill the worker.  The boilerplate is consolidated
here so individual tests stay focused on their assertions.
"""
import asyncio
from contextlib import contextmanager
from multiprocessing import Process

from blackbull.server import ASGIServer


class _LiveAppHandle:
    """What a test ``live`` fixture yields.

    Exposes ``port`` directly (used to construct URLs) and forwards any
    other attribute access to the underlying :class:`BlackBull` instance
    so tests that reach into ``app.router``, ``app._dispatcher``, etc.
    keep working.
    """

    def __init__(self, app, server: ASGIServer):
        self._app = app
        self._server = server
        self.port = server.port

    def __getattr__(self, name):
        return getattr(self._app, name)


@contextmanager
def live_server(app, **server_kwargs):
    """Bind ``app`` to an ephemeral port, fork a worker, yield a handle.

    Usage in a per-test fixture::

        @pytest.fixture(scope="module")
        def live():
            app = _make_app()
            with live_server(app) as handle:
                yield handle

    Tests then read ``live.port`` (or any attribute on the underlying
    ``BlackBull`` instance) and construct URLs against
    ``http://127.0.0.1:{live.port}``.
    """
    server = ASGIServer(app, **server_kwargs)
    server.open_socket(0)
    p = Process(target=lambda: asyncio.run(server.run()))
    p.start()
    try:
        server.wait_for_port(timeout=10.0)
        yield _LiveAppHandle(app, server)
    finally:
        server.close()
        p.terminate()
        p.join(timeout=5)
