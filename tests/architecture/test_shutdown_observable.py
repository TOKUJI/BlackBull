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
from blackbull.server.multiworker import MultiWorkerServer


@pytest.fixture()
def slow_app():
    """A handler slow enough that a shutdown can arrive mid-request."""
    app = BlackBull()

    @app.route(path='/slow', methods=[HTTPMethod.GET])
    async def slow():
        import asyncio
        await asyncio.sleep(1.5)
        return b'finished'

    @app.route(path='/ping', methods=[HTTPMethod.GET])
    async def ping():
        return b'pong'

    return app


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


def test_a_request_in_flight_when_shutdown_starts_still_finishes(slow_app):
    """Graceful means the request completes, not that the process waits.

    Asserted on the client's side — a body, not a process state — because that
    is what an operator draining a node actually observes.
    """
    sock, port = _listener()
    mws = MultiWorkerServer(slow_app, [sock], None, workers=2)
    mws._spawn_all()
    time.sleep(0.8)

    result: dict = {}

    def call():
        try:
            result['value'] = _get(port, '/slow')
        except Exception as exc:                      # noqa: BLE001
            result['error'] = f'{type(exc).__name__}: {exc}'

    caller = threading.Thread(target=call)
    caller.start()
    time.sleep(0.5)                                   # handler is mid-sleep

    try:
        mws._shutdown_all()                           # SIGTERM while in flight
    finally:
        caller.join(timeout=15)
        sock.close()

    assert 'error' not in result, result.get('error')
    assert result.get('value') == (200, b'finished'), result


def test_a_replacement_worker_serves_requests(slow_app):
    """Respawn is only respawn if the new process can answer.

    `test_crashed_worker_is_respawned` asserts a live PID; this asserts the
    listener was actually re-adopted, which is the part a sequencing change
    can break without changing any count.
    """
    sock, port = _listener()
    mws = MultiWorkerServer(slow_app, [sock], None, workers=1)
    mws._spawn_all()
    time.sleep(0.8)
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
    mws = MultiWorkerServer(slow_app, [sock], None, workers=1)
    mws._spawn_all()
    time.sleep(0.8)
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
