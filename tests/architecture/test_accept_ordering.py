"""What may reach an app before its lifespan startup has completed: nothing.

A 200 from an app whose startup hook is still parked is worse than a refused
connection — the client cannot tell it apart from a healthy answer, so it
caches and acts on state the app has not finished building.  These tests pin
the ordering from outside the server: bytes on the wire, and the app's own
handler count.  How the ordering is implemented is deliberately not visible
here, so any mechanism that keeps a client from being served early passes.

``tests/architecture/test_shutdown_observable.py`` is the same shape for the
other end of the lifecycle.
"""
from __future__ import annotations

import asyncio
import contextlib
import time
from contextlib import asynccontextmanager
from functools import partial
from http import HTTPMethod
from pathlib import Path

import pytest

from blackbull import BlackBull
from blackbull.env import reset_settings_cache
from blackbull.server.server import Server

REQUEST = b'GET / HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n'
BODY = b'ordered'

#: What a client may observe while startup is incomplete: the connection is
#: refused, or it is established and stays silent for the read deadline.
SILENT = ('refused', 'no-bytes')


def _app_with_startup(started: asyncio.Event, release: asyncio.Event,
                      calls: list, *, fail: bool = False):
    """A BlackBull app whose startup hook parks until *release* is set."""
    app = BlackBull()

    @app.on_startup
    async def _startup():
        started.set()
        await release.wait()
        if fail:
            raise RuntimeError('startup blew up')

    @app.route(path='/', methods=[HTTPMethod.GET])
    async def _index():
        calls.append(1)
        return BODY

    return app


def _tcp_connector(port: int):
    return partial(asyncio.open_connection, '127.0.0.1', port)


def _unix_connector(path: Path):
    return partial(asyncio.open_unix_connection, str(path))


async def _probe(connect, *, read_timeout: float = 0.5) -> tuple[str, bytes]:
    """One client's view: connect, send a GET, read inside the deadline."""
    try:
        reader, writer = await asyncio.wait_for(connect(), timeout=read_timeout)
    except asyncio.TimeoutError:
        return 'connect-timeout', b''
    except OSError as exc:
        return 'refused', f'{type(exc).__name__}: {exc}'.encode()
    try:
        writer.write(REQUEST)
        with contextlib.suppress(OSError):
            await writer.drain()
        try:
            return 'read', await asyncio.wait_for(reader.read(4096),
                                                  timeout=read_timeout)
        except asyncio.TimeoutError:
            return 'no-bytes', b''
        except OSError as exc:
            return f'read-failed({type(exc).__name__})', b''
    finally:
        writer.close()
        with contextlib.suppress(Exception):
            await asyncio.wait_for(writer.wait_closed(), timeout=2)


async def _await_served(connect, *, timeout: float = 5.0) -> bytes:
    """Retry until the app answers 200, rather than sleeping a fixed guess."""
    deadline = time.monotonic() + timeout
    while True:
        outcome, data = await _probe(connect, read_timeout=0.5)
        if b'HTTP/1.1 200' in data:
            return data
        if time.monotonic() >= deadline:
            raise AssertionError(
                f'no 200 within {timeout}s; last observation: {outcome}')
        await asyncio.sleep(0.05)


async def _await_path(path: Path, *, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return
        await asyncio.sleep(0.02)
    raise AssertionError(f'{path} never appeared within {timeout}s')


@asynccontextmanager
async def _serving(server: Server):
    """``run()`` as a task, with the stop/settle/close sequence guaranteed.

    ``close_socket()`` runs only after the body has finished, so a body that
    observes the port as unreachable is evidence about what ``run()`` and
    ``stop()`` did: closing it here first would make that observation true
    however they behaved.
    """
    runner = asyncio.create_task(server.run())
    try:
        yield runner
    finally:
        await asyncio.wait_for(server.stop(drain_timeout=1.0), timeout=5)
        # Settle rather than await: a startup failure surfaces here, and the
        # test that expects it has already asserted on the exception.
        await asyncio.wait({runner}, timeout=5)
        server.close_socket()


def _listening_server(app, tmp_path: Path | None = None):
    """A bound server plus the connector that reaches it."""
    server = Server(app)
    if tmp_path is None:
        server.open_socket(0)
        return server, _tcp_connector(server.port)
    path = tmp_path / 'accept-ordering.sock'
    server.open_socket(unix_path=str(path))
    return server, _unix_connector(path)


@pytest.mark.asyncio
@pytest.mark.timeout(60)
@pytest.mark.parametrize('transport', ['tcp', 'af_unix'])
async def test_a_request_before_startup_completes_is_not_served(
        transport, tmp_path):
    """The window before ``startup`` completes answers nothing.

    Also the positive control for the other three assertions in this file:
    once the release event fires, a *fresh* connection must be answered, so
    "serve nothing, ever" cannot pass this test.
    """
    started, release, calls = asyncio.Event(), asyncio.Event(), []
    app = _app_with_startup(started, release, calls)
    server, connect = _listening_server(
        app, tmp_path if transport == 'af_unix' else None)

    async with _serving(server):
        await asyncio.wait_for(started.wait(), timeout=5)

        window = await _probe(connect, read_timeout=0.5)
        assert window[0] in SILENT, (
            f'observation during the startup window: {window!r}')
        assert b'HTTP/1.1' not in window[1], (
            f'the startup window answered: {window[1][:64]!r}')

        # Wire silence is not enough: a handler that starts work and only
        # writes later is still a request processed before startup finished.
        assert calls == [], (
            f'{len(calls)} request(s) entered the app before startup completed')

        release.set()
        body = await _await_served(connect)
        assert BODY in body, body


@pytest.mark.asyncio
@pytest.mark.timeout(60)
async def test_a_lifespan_ignoring_asgi_callable_is_still_served(monkeypatch):
    """An app that never acks startup means "lifespan unsupported", not "wait".

    A raw ASGI callable returns from the lifespan scope without sending
    ``lifespan.startup.complete``; ``LifespanManager`` treats that as an app
    with no lifespan and serves it.  An implementation that waits for the ack
    itself, rather than for the manager, stops here forever.
    """
    monkeypatch.setenv('BB_FORCE_ASGI_SCOPE', '1')
    reset_settings_cache()

    async def raw_app(scope, receive, send):
        if scope['type'] != 'http':
            return
        await send({'type': 'http.response.start', 'status': 200,
                    'headers': [(b'content-length', str(len(BODY)).encode())]})
        await send({'type': 'http.response.body', 'body': BODY})

    server = Server(raw_app)
    server.open_socket(0)

    async with _serving(server):
        body = await _await_served(_tcp_connector(server.port))
        assert BODY in body, body


@pytest.mark.asyncio
@pytest.mark.timeout(60)
async def test_a_failed_startup_serves_no_request():
    """Startup that raises must serve zero requests, during and after."""
    started, release, calls = asyncio.Event(), asyncio.Event(), []
    app = _app_with_startup(started, release, calls, fail=True)
    server, connect = _listening_server(app)

    async with _serving(server) as runner:
        await asyncio.wait_for(started.wait(), timeout=5)
        window = await _probe(connect, read_timeout=0.5)
        release.set()
        with pytest.raises(RuntimeError, match='startup blew up'):
            await asyncio.wait_for(runner, timeout=5)

        # ``run()`` has returned and nothing here has closed the listener, so
        # the socket being gone is what ``run()`` did on its way out.  The
        # window may be either observation; after the failure it must refuse.
        after = await _probe(connect, read_timeout=0.5)

    assert window[0] in SILENT, f'during failed startup: {window!r}'
    assert after[0] == 'refused', f'after failed startup: {after!r}'
    assert b'HTTP/1.1' not in window[1] + after[1]
    assert calls == [], f'{len(calls)} request(s) served by a failed app'


@pytest.mark.asyncio
@pytest.mark.timeout(60)
async def test_stop_during_startup_ends_run_without_error():
    """``stop()`` arriving before startup completes must wind down cleanly."""
    started, release, calls = asyncio.Event(), asyncio.Event(), []
    app = _app_with_startup(started, release, calls)
    server, connect = _listening_server(app)

    async with _serving(server) as runner:
        await asyncio.wait_for(started.wait(), timeout=5)
        stopper = asyncio.create_task(server.stop(drain_timeout=1.0))
        release.set()
        await asyncio.wait_for(runner, timeout=5)
        await asyncio.wait_for(stopper, timeout=5)

        after = await _probe(connect, read_timeout=0.5)

    assert after[0] == 'refused', f'still reachable after run(): {after!r}'


@pytest.mark.asyncio
@pytest.mark.timeout(60)
async def test_run_does_not_return_before_on_shutdown_completes(tmp_path):
    """The other end of the ordering: ``run()`` outlives its shutdown hook."""
    release = asyncio.Event()
    sentinel = tmp_path / 'shutdown-entered'

    app = BlackBull()

    @app.on_shutdown
    async def _stop():
        sentinel.write_text('entered')
        await release.wait()

    @app.route(path='/', methods=[HTTPMethod.GET])
    async def _index():
        return BODY

    server, connect = _listening_server(app)

    async with _serving(server) as runner:
        await _await_served(connect)
        stopper = asyncio.create_task(server.stop(drain_timeout=1.0))
        await _await_path(sentinel)
        # ``asyncio.wait``, not ``wait_for``: an expired ``wait_for`` would
        # cancel the runner whose completion is the thing being asserted.
        await asyncio.wait({runner}, timeout=0.3)
        assert not runner.done(), (
            'run() returned while on_shutdown was still parked')

        release.set()
        await asyncio.wait_for(runner, timeout=5)
        await asyncio.wait_for(stopper, timeout=5)

        after = await _probe(connect, read_timeout=0.5)

    assert sentinel.exists(), 'on_shutdown never ran'
    assert after[0] == 'refused', f'still reachable after run(): {after!r}'
