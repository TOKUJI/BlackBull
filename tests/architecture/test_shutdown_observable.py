"""What a shutdown must keep doing, pinned before the sequencing is changed.

Sprint 112 rewrites what the loop does with work still on it when the process
is asked to stop.  That is the one area where a wrong move costs graceful
shutdown, worker restart and `--reload` at once — so each of the three needs a
test that fails if its *observable* behaviour changes, not merely if a process
count does.

The gap these fill: `test_multiworker.py` asserts that a crashed worker is
replaced by a live process, and that workers answer HTTP.  Nothing asserted
that a request already in flight when SIGTERM lands is allowed to finish, and
nothing asserted that a *replacement* worker can actually serve — only that it
is alive.
"""
from __future__ import annotations

import http.client
import socket
import threading
import time
from http import HTTPMethod

import pytest

from blackbull import BlackBull
from blackbull.server.listener import Listener, Tcp
from blackbull.server.multiworker import MultiWorkerServer


@pytest.fixture()
def slow_app():
    """A handler slow enough that a shutdown can arrive mid-request."""
    app = BlackBull()

    @app.route(path='/slow', methods=[HTTPMethod.GET])
    async def slow():
        import asyncio
        import os
        # Say the handler is running before it sleeps, so a test can send the
        # signal *during* the request rather than guessing when that is.
        started = os.environ.get('BB_TEST_STARTED_MARKER')
        if started:
            open(started, 'w').close()
        await asyncio.sleep(1.5)
        return b'finished'

    @app.route(path='/ping', methods=[HTTPMethod.GET])
    async def ping():
        return b'pong'

    return app


def _await_ready(port: int, timeout: float = 20.0) -> None:
    """Block until a worker answers, rather than guessing how long boot takes.

    A fixed sleep is both slower and less reliable: on a loaded runner the
    workers may not be accepting yet when it expires, and the request the test
    means to catch mid-flight is instead refused by a listener that has already
    closed.  That is what made this file fail on three of four CI Pythons.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if _get(port, '/ping', timeout=2)[0] == 200:
                return
        except OSError:
            time.sleep(0.05)
    raise AssertionError(f'no worker answered /ping on {port} within {timeout}s')


def _await_file(path, timeout: float = 20.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if path.exists():
            return
        time.sleep(0.02)
    raise AssertionError(f'{path} never appeared within {timeout}s')


def _listener() -> tuple[socket.socket, int]:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(('127.0.0.1', 0))
    sock.listen()
    return sock, sock.getsockname()[1]


def _get(port: int, path: str, timeout: float = 10.0):
    conn = http.client.HTTPConnection('127.0.0.1', port, timeout=timeout)
    try:
        conn.request('GET', path)
        resp = conn.getresponse()
        return resp.status, resp.read()
    finally:
        conn.close()


def test_a_request_in_flight_when_shutdown_starts_still_finishes(
        slow_app, tmp_path, monkeypatch):
    """Graceful means the request completes, not that the process waits.

    Asserted on the client's side — a body, not a process state — because that
    is what an operator draining a node actually observes.
    """
    marker = tmp_path / 'handler-started'
    monkeypatch.setenv('BB_TEST_STARTED_MARKER', str(marker))
    sock, port = _listener()
    mws = MultiWorkerServer(slow_app, [(Listener(Tcp(port)), [sock])], None, workers=2)
    mws._spawn_all()
    _await_ready(port)

    result: dict = {}

    def call():
        try:
            result['value'] = _get(port, '/slow')
        except Exception as exc:                      # noqa: BLE001
            result['error'] = f'{type(exc).__name__}: {exc}'

    caller = threading.Thread(target=call)
    caller.start()
    _await_file(marker)                               # the handler is running

    try:
        mws._shutdown_all()                           # SIGTERM while in flight
    finally:
        caller.join(timeout=15)
        sock.close()

    finished = marker.with_suffix('.finished')
    assert 'error' not in result, (
        f"{result.get('error')} — handler {'completed' if finished.exists() else 'was cut short'}, "
        f"which says whether the drain ran or the response was lost after it")
    assert result.get('value') == (200, b'finished'), result


def test_a_replacement_worker_serves_requests(slow_app):
    """Respawn is only respawn if the new process can answer.

    `test_crashed_worker_is_respawned` asserts a live PID; this asserts the
    listener was actually re-adopted, which is the part a sequencing change
    can break without changing any count.
    """
    sock, port = _listener()
    mws = MultiWorkerServer(slow_app, [(Listener(Tcp(port)), [sock])], None, workers=1)
    mws._spawn_all()
    _await_ready(port)
    try:
        assert _get(port, '/ping') == (200, b'pong'), 'baseline'

        victim = mws._processes[0]
        original_pid = victim.pid
        victim.kill()
        victim.join(timeout=5)
        mws._reap_and_respawn()

        assert mws._processes[0].pid != original_pid
        deadline = time.monotonic() + 10
        last = None
        while time.monotonic() < deadline:
            try:
                last = _get(port, '/ping', timeout=2)
                if last == (200, b'pong'):
                    break
            except Exception as exc:                  # noqa: BLE001
                last = f'{type(exc).__name__}: {exc}'
            time.sleep(0.2)
        assert last == (200, b'pong'), f'replacement never served: {last}'
    finally:
        mws._shutdown_all()
        sock.close()


def test_the_listening_socket_survives_a_worker_generation(slow_app):
    """The port stays bound across a respawn.

    `--reload` depends on this same property through a different route (fd
    inheritance across `execvp`); a shutdown change that closes listeners
    early breaks both, and this is the cheaper of the two to run.
    """
    sock, port = _listener()
    mws = MultiWorkerServer(slow_app, [(Listener(Tcp(port)), [sock])], None, workers=1)
    mws._spawn_all()
    _await_ready(port)
    try:
        mws._processes[0].kill()
        mws._processes[0].join(timeout=5)

        # Between generations the socket must still refuse a rebind: something
        # still holds it.  A closed listener would let this bind succeed.
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            with pytest.raises(OSError):
                probe.bind(('127.0.0.1', port))
        finally:
            probe.close()

        mws._reap_and_respawn()
    finally:
        mws._shutdown_all()
        sock.close()


@pytest.mark.asyncio
async def test_run_does_not_return_while_a_drain_is_holding_a_request(caplog):
    """``run()`` must not return while a request is still being answered.

    **This does not reproduce the CI failure.**  It passes with the fix removed:
    locally the drain always wins the race, whether ``stop()`` is awaited inline
    or run as a task.  It is kept as a statement of the contract, not as the
    regression guard — the guard is
    ``test_a_request_in_flight_when_shutdown_starts_still_finishes`` under CI,
    which is the only environment that has produced the losing order.

    ``stop()`` closes the listeners, which is what ends ``serve_forever()`` and
    unwinds the TaskGroup in ``run()``.  If ``run()`` returns there, it returns
    into ``asyncio.run()``, which cancels every remaining task — the drain, and
    the request the drain exists to protect.  The handler is then cut short and
    the client sees a closed connection, which is precisely what a graceful
    shutdown promises not to do.

    Driven directly rather than through a fork and a signal, so the ordering is
    the same but the timing is not a guess.
    """
    import asyncio
    import logging

    from blackbull.server import sender as _sender
    from blackbull.server.server import Server

    # The send path skips a write when the transport is already closing, and
    # the access log is emitted in a finally either way -- so a dropped
    # response looks like a served one from the server's side.  Open the gate
    # that reports the skip, so this test can tell them apart.
    caplog.set_level(logging.DEBUG, logger='blackbull.server.sender')
    monkeypatch_debug = getattr(_sender, '_DEBUG', None)
    _sender._DEBUG = True

    started = asyncio.Event()
    finished = False

    app = BlackBull()

    @app.route(path='/slow', methods=[HTTPMethod.GET])
    async def slow():
        nonlocal finished
        started.set()
        await asyncio.sleep(0.5)
        finished = True
        return b'finished'

    server = Server(app)
    server.open_socket(0)
    port = server.port
    runner = asyncio.create_task(server.run())
    await asyncio.sleep(0.4)

    caller = asyncio.create_task(asyncio.to_thread(_get, port, '/slow'))
    await asyncio.wait_for(started.wait(), timeout=10)

    # As a task, the way the worker's signal handler starts it -- awaiting it
    # inline would let the drain finish before run() ever unwinds, which is
    # the ordering the bug does not happen in.
    stopper = asyncio.create_task(server.stop(drain_timeout=8.0))
    await asyncio.wait_for(runner, timeout=10)

    assert finished, ('run() returned while the handler was still running; in a '
                      'worker that returns into asyncio.run(), which cancels it')
    await asyncio.wait_for(stopper, timeout=10)

    # The handler finishing and the client receiving are different claims, and
    # CI has shown them coming apart: an access-logged 200 with the client
    # seeing nothing.  Report which side of the socket lost it.
    try:
        answer = await asyncio.wait_for(caller, timeout=10)
    except Exception as exc:                              # noqa: BLE001
        skipped = [r.getMessage() for r in caplog.records
                   if 'write skipped' in r.getMessage()]
        raise AssertionError(
            f'handler completed ({finished}) and the server logged a response, '
            f'but the client got {type(exc).__name__}: {exc}. '
            f'send-path skips: {skipped or "none"} — a skip means the guard '
            f'saw the transport already closing; none means it was lost later'
        ) from exc
    finally:
        if monkeypatch_debug is not None:
            _sender._DEBUG = monkeypatch_debug
    assert answer == (200, b'finished'), answer
    server.close_socket()

