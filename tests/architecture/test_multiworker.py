"""Integration tests for the multi-worker server (blackbull/server/multiworker.py
and blackbull/server/worker.py).

These tests start real child processes and make real HTTP connections so that
the full fork→socket→asyncio pipeline is exercised.  Each test is isolated:
sockets are bound to ephemeral ports (port 0) so tests never collide.

Test plan
---------
T1  MultiWorkerServer spawns exactly *workers* processes on start.
T2  A crashed worker is automatically respawned within the monitor interval.
T3  Workers actually serve HTTP/1.1 requests (end-to-end smoke test).
T4  BlackBull.serve() single-worker path calls asyncio.run (regression guard).
T5  MultiWorkerServer.run() exits cleanly after SIGTERM to the master.
"""
import asyncio
import os
import signal
import socket
import ssl
import time
import threading
import http.client
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from blackbull import BlackBull
from blackbull.server.server import ASGIServer
from blackbull.server.listener import Listener, Tcp
from blackbull.server.multiworker import MultiWorkerServer


def as_listeners(socks, speaks='http', workers=None):
    """Wrap already-bound sockets the way the master hands them over."""
    return [(Listener(Tcp(s.getsockname()[1]), speaks=speaks, workers=workers),
             [s]) for s in socks]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def plain_app():
    """Minimal ASGI app that responds 200 OK with b'ok'."""
    app = BlackBull()

    from http import HTTPMethod
    @app.route(path='/ping', methods=[HTTPMethod.GET])
    async def ping():
        return b'pong'

    return app


@pytest.fixture()
def bound_sockets():
    """Yield a list of ephemeral plain-TCP sockets and close them on teardown."""
    import socket
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(('127.0.0.1', 0))
    sock.listen()
    port = sock.getsockname()[1]
    yield [sock], port
    sock.close()


# ---------------------------------------------------------------------------
# T1 — correct number of workers spawned
# ---------------------------------------------------------------------------

def test_spawn_count(plain_app, bound_sockets):
    """MultiWorkerServer must spawn exactly *workers* processes."""
    sockets, _ = bound_sockets
    mws = MultiWorkerServer(plain_app, as_listeners(sockets), None, workers=3)

    mws._spawn_all()
    try:
        assert len(mws._processes) == 3
        for p in mws._processes:
            assert p.is_alive(), 'Every worker must be alive right after spawn'
    finally:
        mws._shutdown_all()


# ---------------------------------------------------------------------------
# T2 — crashed worker is respawned
# ---------------------------------------------------------------------------

def test_crashed_worker_is_respawned(plain_app, bound_sockets):
    """A worker that exits unexpectedly must be replaced within one monitor cycle."""
    sockets, _ = bound_sockets
    mws = MultiWorkerServer(plain_app, as_listeners(sockets), None, workers=2)

    mws._spawn_all()
    try:
        victim = mws._processes[0]
        original_pid = victim.pid
        victim.kill()
        victim.join(timeout=2)

        # One monitor cycle
        mws._reap_and_respawn()

        new_proc = mws._processes[0]
        assert new_proc.is_alive(), 'Replacement worker must be alive'
        assert new_proc.pid != original_pid, 'Replacement must be a new process'
    finally:
        mws._shutdown_all()


# ---------------------------------------------------------------------------
# T3 — end-to-end HTTP smoke test
# ---------------------------------------------------------------------------

def test_workers_serve_http_requests(plain_app):
    """Workers must handle real HTTP/1.1 GET requests via inherited sockets."""
    # Bind a plain-TCP socket on an ephemeral port.
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(('127.0.0.1', 0))
    sock.listen()
    port = sock.getsockname()[1]

    mws = MultiWorkerServer(plain_app, as_listeners([sock]), None, workers=2)
    mws._spawn_all()

    # Give workers a moment to enter their event loops.
    time.sleep(0.5)

    try:
        conn = http.client.HTTPConnection('127.0.0.1', port, timeout=5)
        conn.request('GET', '/ping')
        resp = conn.getresponse()
        body = resp.read()
        conn.close()
        assert resp.status == 200
        assert body == b'pong'
    finally:
        mws._shutdown_all()
        sock.close()


# ---------------------------------------------------------------------------
# T4 — BlackBull.run() single-worker uses asyncio.run (regression guard)
# ---------------------------------------------------------------------------

def test_run_single_worker_uses_asyncio_run(plain_app):
    """app.run(workers=1) must delegate to asyncio.run, not MultiWorkerServer."""
    with patch('blackbull.app.asyncio.run') as mock_asyncio_run:
        # asyncio.run won't actually run the coroutine in the mock, so
        # we just verify it was called.
        plain_app.run(port=9999, workers=1)
        assert mock_asyncio_run.called, 'asyncio.run must be called for workers=1'


# ---------------------------------------------------------------------------
# T5 — master shuts down cleanly on SIGTERM
# ---------------------------------------------------------------------------

def test_master_stops_on_sigterm(plain_app, bound_sockets):
    """Sending SIGTERM to the master (current process simulated) must stop run()."""
    sockets, _ = bound_sockets
    mws = MultiWorkerServer(plain_app, as_listeners(sockets), None, workers=2,
                            shutdown_timeout=3.0)

    # Run the master in a thread so this test's event loop is unaffected.
    result = {}

    def _run():
        try:
            mws.run()
            result['exited'] = True
        except Exception as exc:
            result['error'] = exc

    t = threading.Thread(target=_run, daemon=True)
    t.start()

    # Give the master a moment to spawn workers and enter its monitor loop.
    time.sleep(0.5)

    # Simulate SIGTERM to the master (the master installed signal handlers
    # that set _stopped).  We can trigger this by setting the flag directly
    # since we're in the same process as the thread.
    mws._stopped = True

    t.join(timeout=15)
    assert not t.is_alive(), 'Master thread must exit after _stopped is set'
    assert result.get('exited'), f"Master did not exit cleanly: {result.get('error')}"


@pytest.mark.timeout(30)
def test_sigterm_during_startup_is_not_lost(plain_app, bound_sockets):
    """A stop signal arriving before the supervision loop starts must stick.

    ``run()`` installs the handlers first and only then spawns workers and
    (under reload) starts the watcher thread, so there is a real window in
    which the handler can fire before the loop is reached.  T5 above sleeps
    0.5 s before signalling, which steps over that window; here the signal
    is delivered from inside the spawn, squarely within it.

    Real delivery matters, so this runs ``run()`` on the main thread —
    ``_install_signal_handlers`` is a no-op anywhere else, which is why T5
    has to poke the flag by hand.
    """
    if threading.current_thread() is not threading.main_thread():
        pytest.skip('signal handlers only install on the main thread')

    sockets, _ = bound_sockets
    mws = MultiWorkerServer(plain_app, as_listeners(sockets), None, workers=1,
                            shutdown_timeout=3.0)

    # Stubbed: this test is about the flag, not about real children.
    mws._spawn_all = lambda: os.kill(os.getpid(), signal.SIGTERM)
    mws._shutdown_all = lambda: None

    # Guarantees termination either way, so a regression fails on an
    # assertion rather than hanging until the timeout.
    rescued = threading.Event()

    def _rescue() -> None:
        rescued.set()
        mws._stopped = True

    timer = threading.Timer(2.0, _rescue)
    previous = {
        signal.SIGTERM: signal.getsignal(signal.SIGTERM),
        signal.SIGINT: signal.getsignal(signal.SIGINT),
    }
    timer.start()
    try:
        mws.run()
    finally:
        timer.cancel()
        for signo, handler in previous.items():
            signal.signal(signo, handler)

    assert not rescued.is_set(), (
        'run() kept supervising after a SIGTERM delivered during startup: '
        'the stop flag the handler set was overwritten before the loop '
        'read it, so the shutdown request was silently dropped'
    )


# ---------------------------------------------------------------------------
# HTTP scales across workers while a stateful protocol
# (here: a raw echo, standing in for MQTT) is owned by worker 0 only.
# ---------------------------------------------------------------------------

@pytest.fixture()
def http_and_raw_app():
    """App with an HTTP route and a port-bound raw echo protocol."""
    app = BlackBull()

    from http import HTTPMethod

    @app.route(path='/ping', methods=[HTTPMethod.GET])
    async def ping():
        return b'pong'

    @app.raw_handler('echo', port=0)
    async def echo(reader, writer, ctx):
        while True:
            data = await reader.read(1024)
            if not data:
                break
            await writer.write(data)

    return app


def test_spawn_worker_routes_protocol_sockets_to_worker0_only(plain_app, bound_sockets):
    """The stateful protocol listeners must be handed to worker 0 only; every
    other worker serves the shared ones only.  Regression guard for the
    single-owner invariant — a broker on >1 worker would scatter state.

    Asserts the invariant rather than an argument position: what must hold is
    that exactly one worker is handed the broker, whatever the handover looks
    like."""
    sockets, _ = bound_sockets
    broker = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    broker.bind(('127.0.0.1', 0))
    broker.listen()
    mws = MultiWorkerServer(
        plain_app,
        as_listeners(sockets) + as_listeners([broker], speaks='mqtt'),
        None, workers=3)

    captured = {}

    class _FakeProc:
        pid = 0

        def __init__(self, *, target, args, daemon, name):
            captured[name] = args

        def start(self):
            pass

    try:
        with patch.object(mws._mp_ctx, 'Process', _FakeProc):
            for worker_id in range(3):
                mws._spawn_worker(worker_id)

        def speaks_of(name):
            listeners = captured[name][1]      # args = (app, listeners, ...)
            return sorted(listener.speaks for listener, _socks in listeners)

        assert speaks_of('bb-worker-0') == ['http', 'mqtt'], 'worker 0 owns the broker'
        assert speaks_of('bb-worker-1') == ['http'], 'worker 1 serves the shared one only'
        assert speaks_of('bb-worker-2') == ['http'], 'worker 2 serves the shared one only'

        owners = sum('mqtt' in speaks_of(f'bb-worker-{i}') for i in range(3))
        assert owners == 1, 'a stateful protocol must have exactly one owner'
    finally:
        broker.close()


def test_multiworker_http_scales_while_raw_stays_on_worker0(http_and_raw_app):
    """End-to-end success criterion: with workers=2 and a port-bound protocol,
    HTTP is served (by any worker) AND the raw protocol round-trips (worker 0)."""
    master = ASGIServer(http_and_raw_app)
    master.open_socket(0)  # binds HTTP + the echo port, both as listeners
    http_port = master.port
    echo_port = master.protocol_ports['echo']

    mws = MultiWorkerServer(
        http_and_raw_app, master.bound_listeners, None, workers=2)
    mws._spawn_all()
    time.sleep(0.6)  # let workers enter their event loops

    try:
        # HTTP is served (shared listener across both workers).
        conn = http.client.HTTPConnection('127.0.0.1', http_port, timeout=5)
        conn.request('GET', '/ping')
        resp = conn.getresponse()
        body = resp.read()
        conn.close()
        assert resp.status == 200
        assert body == b'pong'

        # The raw protocol round-trips — proving worker 0 adopted its listener.
        deadline = time.time() + 5
        while True:
            try:
                raw = socket.create_connection(('127.0.0.1', echo_port), timeout=1)
                break
            except OSError:
                if time.time() >= deadline:
                    raise
                time.sleep(0.05)
        with raw:
            raw.sendall(b'broker-on-worker0\n')
            assert raw.recv(1024) == b'broker-on-worker0\n'
    finally:
        mws._shutdown_all()
        master.close_socket()


# ---------------------------------------------------------------------------
# Serve() no longer forces workers=1 for stateful protocols
# (except under auto-reload, whose exec handoff does not carry them yet).
# ---------------------------------------------------------------------------

def test_serve_keeps_workers_for_port_bindings_without_reload(http_and_raw_app):
    """serve(workers=4) with a port-bound protocol must reach the multi-worker
    path with workers unchanged — HTTP should scale alongside the broker."""
    from blackbull import app as app_mod

    with patch('blackbull.server.multiworker.MultiWorkerServer') as MockMWS:
        MockMWS.return_value.run.return_value = None
        app_mod.serve(http_and_raw_app, port=0, workers=4)

    assert MockMWS.called, 'multi-worker path must be taken (not forced to single-worker)'
    assert MockMWS.call_args.kwargs['workers'] == 4, 'workers must not be downgraded'
    handed = MockMWS.call_args.args[1]
    assert any(listener.speaks == 'echo' for listener, _socks in handed), \
        'broker sockets must be handed off'


def test_serve_forces_single_worker_for_port_bindings_with_reload(http_and_raw_app):
    """Under reload, the protocol-socket exec handoff is not wired, so a
    port-bound protocol still forces the single-worker (asyncio.run) path."""
    from blackbull import app as app_mod

    with patch('blackbull.app.asyncio.run') as mock_run, \
            patch('blackbull.server.multiworker.MultiWorkerServer') as MockMWS:
        # reload=True normally takes the multiworker path, but the port-binding
        # downgrade should pin workers=1 — which, with reload, still routes
        # through MultiWorkerServer at workers=1 (not asyncio.run).  Assert the
        # downgrade by inspecting the workers kwarg.
        MockMWS.return_value.run.return_value = None
        app_mod.serve(http_and_raw_app, port=0, workers=4, reload=True)

    assert MockMWS.called
    assert MockMWS.call_args.kwargs['workers'] == 1, 'reload + protocol must force workers=1'
