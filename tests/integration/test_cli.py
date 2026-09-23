"""End-to-end test for the ``blackbull`` console script.

Spawns the installed ``blackbull`` script as a subprocess against an
on-disk module path and verifies it actually serves traffic.  The CLI's
internals are covered by ``tests/unit/test_cli.py``; this file is the
seam where the ``[project.scripts]`` entry, ``module:attr`` resolution,
and the runtime are wired together.

Plain HTTP (no TLS) for simplicity — the CLI handles ``--certfile``
and ``--keyfile`` via :func:`blackbull.app.serve`, which is already
covered by the multi-worker and reload integration tests.
"""
from __future__ import annotations

import http.client
import os
import shutil
import signal
import socket
import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path

import pytest


_STARTUP_DEADLINE_SEC = 15.0
_REQ_TIMEOUT_SEC = 2.0


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


def _get(port: int, path: str) -> bytes | None:
    try:
        conn = http.client.HTTPConnection('127.0.0.1', port, timeout=_REQ_TIMEOUT_SEC)
        conn.request('GET', path)
        resp = conn.getresponse()
        body = resp.read()
        conn.close()
        return body
    except (OSError, http.client.HTTPException):
        return None


def _wait_until(predicate, deadline: float, poll: float = 0.1):
    while time.monotonic() < deadline:
        result = predicate()
        if result:
            return result
        time.sleep(poll)
    return None


@pytest.mark.timeout(45)
def test_cli_serves_blackbull_app(tmp_path: Path):
    """``blackbull module:app`` resolves a BlackBull instance and serves it."""
    blackbull = shutil.which('blackbull')
    assert blackbull, "'blackbull' console script is not on PATH — run 'pip install -e .'"

    port = _free_port()
    script = tmp_path / 'cli_app.py'
    script.write_text(textwrap.dedent('''
        from blackbull import BlackBull

        app = BlackBull()

        @app.route(path='/version')
        async def version():
            return b'cli-v1'
    ''').lstrip())

    env = os.environ.copy()
    env['BB_ACCESS_LOG'] = '0'
    env['PYTHONUNBUFFERED'] = '1'

    log_path = tmp_path / 'subprocess.log'
    log_fh = open(log_path, 'w', buffering=1)

    proc = subprocess.Popen(
        [blackbull, 'cli_app:app', '--bind', f'127.0.0.1:{port}'],
        env=env,
        cwd=str(tmp_path),
        stdout=log_fh,
        stderr=subprocess.STDOUT,
        text=True,
        start_new_session=True,
    )

    try:
        body = _wait_until(
            lambda: _get(port, '/version'),
            deadline=time.monotonic() + _STARTUP_DEADLINE_SEC,
        )
        assert body == b'cli-v1', f'expected b"cli-v1", got {body!r}'
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
        log_fh.close()
        if log_path.exists() and log_path.stat().st_size > 0:
            print(f'--- subprocess log ({log_path.stat().st_size} bytes) ---')
            print(log_path.read_text()[-4000:])


@pytest.mark.timeout(45)
def test_cli_serves_over_unix_domain_socket(tmp_path: Path):
    """``blackbull module:app --bind unix:/path`` serves traffic through AF_UNIX.

    Nginx → BlackBull deployments use UDS to avoid exposing
    a TCP port.  Verify the CLI parses the spec, ASGIServer binds AF_UNIX,
    and a client can complete a request through the socket file.
    """
    blackbull = shutil.which('blackbull')
    assert blackbull, "'blackbull' console script is not on PATH"

    sock_path = tmp_path / 'bb.sock'
    script = tmp_path / 'uds_app.py'
    script.write_text(textwrap.dedent('''
        from blackbull import BlackBull
        app = BlackBull()
        @app.route(path='/version')
        async def version():
            return b'uds-v1'
    ''').lstrip())

    env = os.environ.copy()
    env['BB_ACCESS_LOG'] = '0'
    env['PYTHONUNBUFFERED'] = '1'

    log_path = tmp_path / 'subprocess.log'
    log_fh = open(log_path, 'w', buffering=1)

    proc = subprocess.Popen(
        [blackbull, 'uds_app:app', '--bind', f'unix:{sock_path}'],
        env=env,
        cwd=str(tmp_path),
        stdout=log_fh,
        stderr=subprocess.STDOUT,
        text=True,
        start_new_session=True,
    )

    def _get_via_uds() -> bytes | None:
        if not sock_path.exists():
            return None
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sk:
                sk.settimeout(_REQ_TIMEOUT_SEC)
                sk.connect(str(sock_path))
                sk.sendall(b'GET /version HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n')
                chunks: list[bytes] = []
                while True:
                    chunk = sk.recv(4096)
                    if not chunk:
                        break
                    chunks.append(chunk)
                raw = b''.join(chunks)
            head, _, body = raw.partition(b'\r\n\r\n')
            return body if b' 200 ' in head.split(b'\r\n', 1)[0] else None
        except OSError:
            return None

    try:
        body = _wait_until(
            _get_via_uds,
            deadline=time.monotonic() + _STARTUP_DEADLINE_SEC,
        )
        assert body == b'uds-v1', f'expected b"uds-v1", got {body!r}'
        # Socket file should exist on disk (real bind, not stub).
        assert sock_path.exists()
        # And it must be a socket — not a regular file overwritten by accident.
        import stat as _stat
        assert _stat.S_ISSOCK(sock_path.stat().st_mode)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
        log_fh.close()
        if log_path.exists() and log_path.stat().st_size > 0:
            print(f'--- subprocess log ({log_path.stat().st_size} bytes) ---')
            print(log_path.read_text()[-4000:])


@pytest.mark.timeout(45)
def test_cli_serves_raw_asgi_callable(tmp_path: Path):
    """``blackbull`` can serve a plain ASGI callable (no BlackBull instance).

    This is the path the benchmark harness takes —
    pointing the CLI at ``bench.peers.asgi_app:app`` (a raw ASGI app)
    just like every other peer server.
    """
    blackbull = shutil.which('blackbull')
    assert blackbull, "'blackbull' console script is not on PATH"

    port = _free_port()
    script = tmp_path / 'raw_asgi_app.py'
    script.write_text(textwrap.dedent('''
        # Bare-bones ASGI 3.0 callable — no framework.
        async def app(scope, receive, send):
            if scope['type'] != 'http':
                return
            if scope['path'] == '/raw':
                await send({'type': 'http.response.start', 'status': 200,
                            'headers': [(b'content-type', b'text/plain')]})
                await send({'type': 'http.response.body', 'body': b'raw-ok'})
            else:
                await send({'type': 'http.response.start', 'status': 404,
                            'headers': []})
                await send({'type': 'http.response.body', 'body': b''})
    ''').lstrip())

    env = os.environ.copy()
    env['BB_ACCESS_LOG'] = '0'
    env['PYTHONUNBUFFERED'] = '1'
    # BlackBull is a native-Connection framework — its server hands
    # the app a typed ``Connection`` by default. A *raw* ASGI callable (no
    # BlackBull instance) reads ``scope['type']``/``scope['path']``, so it must
    # opt into the ASGI-scope compat lane via ``BB_FORCE_ASGI_SCOPE=1``.
    env['BB_FORCE_ASGI_SCOPE'] = '1'

    log_path = tmp_path / 'subprocess.log'
    log_fh = open(log_path, 'w', buffering=1)

    proc = subprocess.Popen(
        [blackbull, 'raw_asgi_app:app', '--bind', f'127.0.0.1:{port}'],
        env=env,
        cwd=str(tmp_path),
        stdout=log_fh,
        stderr=subprocess.STDOUT,
        text=True,
        start_new_session=True,
    )

    try:
        body = _wait_until(
            lambda: _get(port, '/raw'),
            deadline=time.monotonic() + _STARTUP_DEADLINE_SEC,
        )
        assert body == b'raw-ok', f'expected b"raw-ok", got {body!r}'
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
        log_fh.close()
        if log_path.exists() and log_path.stat().st_size > 0:
            print(f'--- subprocess log ({log_path.stat().st_size} bytes) ---')
            print(log_path.read_text()[-4000:])


# ---------------------------------------------------------------------------
# Exit status and signals
# ---------------------------------------------------------------------------
# ``python -c`` into ``cli.main``, not the console script: the status read
# must be the entry point's own.

_SHUTDOWN_APP = '''
import asyncio, os, signal, sys

from blackbull import BlackBull

app = BlackBull()


@app.route(path='/ready')
async def ready():
    return b'ready'


@app.route(path='/slow')
async def slow():
    await asyncio.sleep({hold})
    return b'slow-done'


if os.environ.get('PARK_STARTUP'):
    @app.on_startup
    async def _park():
        print('STARTUP_PARKED', file=sys.stderr, flush=True)
        await asyncio.sleep(float(os.environ['PARK_STARTUP']))


if os.environ.get('OWN_SIGTERM_HANDLER'):
    def _mine(signo, frame):
        print('OWN_HANDLER_RAN', file=sys.stderr, flush=True)
        if os.environ['OWN_SIGTERM_HANDLER'] != 'stay':
            sys.exit(7)

    signal.signal(signal.SIGTERM, _mine)


@app.on_shutdown
async def _flush():
    print('SHUTDOWN_HOOK_RAN', file=sys.stderr, flush=True)
    {body}
'''

_CLI_ENTRY = 'from blackbull.cli import main; raise SystemExit(main())'

_SLOW_SECONDS = 1.0


def _repo_root() -> str:
    import blackbull
    return str(Path(blackbull.__file__).resolve().parent.parent)


def _spawn_server(tmp_path: Path, body: str, port: int,
                  extra_env: dict | None = None) -> subprocess.Popen:
    (tmp_path / 'shutdown_app.py').write_text(
        _SHUTDOWN_APP.format(body=body, hold=_SLOW_SECONDS).lstrip())

    env = os.environ.copy()
    env['BB_ACCESS_LOG'] = '0'
    env['PYTHONUNBUFFERED'] = '1'
    env['PYTHONPATH'] = os.pathsep.join([str(tmp_path), _repo_root()])
    env.update(extra_env or {})

    proc = subprocess.Popen(
        [sys.executable, '-c', _CLI_ENTRY,
         'shutdown_app:app', '--bind', f'127.0.0.1:{port}'],
        env=env, cwd=str(tmp_path), stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True, start_new_session=True,
    )
    if not (extra_env or {}).get('PARK_STARTUP'):
        assert _wait_until(
            lambda: _get(port, '/ready'),
            deadline=time.monotonic() + _STARTUP_DEADLINE_SEC,
        ) == b'ready', 'the server never served a request'
    return proc


def _reap(proc: subprocess.Popen) -> subprocess.CompletedProcess:
    try:
        stdout, stderr = proc.communicate(timeout=30)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.communicate(timeout=5)
    return subprocess.CompletedProcess(proc.args, proc.returncode, stdout, stderr)


def _serve_then_signal(tmp_path: Path, body: str,
                       sig: int) -> subprocess.CompletedProcess:
    port = _free_port()
    proc = _spawn_server(tmp_path, body, port)
    proc.send_signal(sig)
    return _reap(proc)


@pytest.mark.parametrize('sig', [signal.SIGINT, signal.SIGTERM],
                         ids=['sigint', 'sigterm'])
@pytest.mark.timeout(60)
def test_a_failing_shutdown_hook_exits_non_zero(tmp_path: Path, sig: int):
    done = _serve_then_signal(
        tmp_path, "raise RuntimeError('flush to disk failed')", sig)
    assert 'SHUTDOWN_HOOK_RAN' in done.stderr, (
        f'the shutdown hook never ran on {signal.Signals(sig).name}\n'
        f'{done.stderr[-2000:]}')
    assert done.returncode == 1, (
        f'a failed shutdown exited {done.returncode}\n{done.stderr[-2000:]}')
    assert 'flush to disk failed' in done.stderr, done.stderr[-2000:]


@pytest.mark.parametrize('sig', [signal.SIGINT, signal.SIGTERM],
                         ids=['sigint', 'sigterm'])
@pytest.mark.timeout(60)
def test_a_clean_shutdown_hook_still_exits_zero(tmp_path: Path, sig: int):
    done = _serve_then_signal(tmp_path, 'return', sig)
    assert 'SHUTDOWN_HOOK_RAN' in done.stderr, (
        f'the shutdown hook never ran on {signal.Signals(sig).name}\n'
        f'{done.stderr[-2000:]}')
    assert done.returncode == 0, (
        f'a clean shutdown exited {done.returncode}\n{done.stderr[-2000:]}')


@pytest.mark.timeout(60)
def test_sigterm_lets_a_request_in_flight_finish(tmp_path: Path):
    port = _free_port()
    proc = _spawn_server(tmp_path, 'return', port)
    pending: list = []
    caller = threading.Thread(
        target=lambda: pending.append(_get(port, '/slow')), daemon=True)
    caller.start()
    time.sleep(_SLOW_SECONDS / 4)
    proc.send_signal(signal.SIGTERM)
    caller.join(timeout=30)
    done = _reap(proc)
    assert pending == [b'slow-done'], (
        f'the in-flight request did not finish: {pending}\n{done.stderr[-2000:]}')
    assert done.returncode == 0, (
        f'a drained shutdown exited {done.returncode}\n{done.stderr[-2000:]}')


_SIGNAL_PATIENCE = 20.0


def _reap_or_kill(proc: subprocess.Popen) -> tuple[subprocess.CompletedProcess, bool]:
    # ``returncode`` is None until ``communicate()`` returns: read it after.
    killed = False
    try:
        out, err = proc.communicate(timeout=_SIGNAL_PATIENCE)
    except subprocess.TimeoutExpired:
        killed = True
        proc.kill()
        out, err = proc.communicate(timeout=5)
    return subprocess.CompletedProcess(proc.args, proc.returncode, out, err), killed


@pytest.mark.timeout(90)
def test_sigterm_during_a_parked_lifespan_startup_still_stops(tmp_path: Path):
    port = _free_port()
    proc = _spawn_server(tmp_path, 'return', port, {'PARK_STARTUP': '60'})
    time.sleep(1.5)
    assert proc.poll() is None, 'the server exited before the signal'
    started = time.monotonic()
    proc.send_signal(signal.SIGTERM)
    done, killed = _reap_or_kill(proc)
    elapsed = time.monotonic() - started
    assert not killed, (
        f'SIGTERM left the process running; only SIGKILL ended it\n'
        f'{done.stderr[-2000:]}')
    assert 'STARTUP_PARKED' in done.stderr, done.stderr[-2000:]
    assert elapsed < 10.0, f'stopping took {elapsed:.3f}s'
    assert done.returncode == 0, (
        f'a stop requested during startup exited {done.returncode}\n'
        f'{done.stderr[-2000:]}')


@pytest.mark.timeout(90)
def test_an_applications_sigterm_handler_still_runs_and_sets_the_status(
        tmp_path: Path):
    port = _free_port()
    proc = _spawn_server(tmp_path, 'return', port, {'OWN_SIGTERM_HANDLER': '1'})
    proc.send_signal(signal.SIGTERM)
    done, killed = _reap_or_kill(proc)
    assert not killed, done.stderr[-2000:]
    assert 'OWN_HANDLER_RAN' in done.stderr, (
        f"the application's own SIGTERM handler never ran\n{done.stderr[-2000:]}")
    assert done.returncode == 7, (
        f'the application chose 7; got {done.returncode}\n{done.stderr[-2000:]}')


@pytest.mark.timeout(90)
def test_the_shutdown_hook_runs_before_an_applications_sigterm_handler(
        tmp_path: Path):
    port = _free_port()
    proc = _spawn_server(tmp_path, 'return', port, {'OWN_SIGTERM_HANDLER': '1'})
    proc.send_signal(signal.SIGTERM)
    done, killed = _reap_or_kill(proc)
    assert not killed, done.stderr[-2000:]
    assert 'SHUTDOWN_HOOK_RAN' in done.stderr, done.stderr[-2000:]
    assert done.stderr.index('SHUTDOWN_HOOK_RAN') < done.stderr.index('OWN_HANDLER_RAN'), (
        "the application's handler ran before the shutdown it preempted\n"
        f'{done.stderr[-2000:]}')


@pytest.mark.timeout(90)
def test_two_sigterms_run_the_handler_twice_and_the_shutdown_once(tmp_path: Path):
    port = _free_port()
    proc = _spawn_server(tmp_path, 'return', port, {'OWN_SIGTERM_HANDLER': 'stay'})
    pending: list = []
    caller = threading.Thread(
        target=lambda: pending.append(_get(port, '/slow')), daemon=True)
    caller.start()
    time.sleep(_SLOW_SECONDS / 4)
    proc.send_signal(signal.SIGTERM)
    time.sleep(_SLOW_SECONDS / 4)
    proc.send_signal(signal.SIGTERM)
    caller.join(timeout=30)
    done, killed = _reap_or_kill(proc)
    assert not killed, done.stderr[-2000:]
    assert done.stderr.count('OWN_HANDLER_RAN') == 2, (
        f"the application's handler ran "
        f"{done.stderr.count('OWN_HANDLER_RAN')} time(s) for two signals\n"
        f'{done.stderr[-2000:]}')
    assert done.stderr.count('SHUTDOWN_HOOK_RAN') == 1, done.stderr[-2000:]
    assert pending == [b'slow-done'], (
        f'the in-flight request did not finish: {pending}')
