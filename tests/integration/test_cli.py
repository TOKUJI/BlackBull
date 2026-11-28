"""End-to-end test for the ``blackbull`` console script.

Spawns the installed ``blackbull`` script as a subprocess against an
on-disk module path and verifies it actually serves traffic.  The CLI's
internals are covered by ``tests/unit/test_cli.py``; this file is the
seam where the ``[project.scripts]`` entry, ``module:attr`` resolution,
and the runtime are wired together.

TLS is driven through the script too: the CLI builds the listener for a
named ``--bind`` host itself, so it alone decides whether that port speaks
TLS (BLA-728).
"""
from __future__ import annotations

import http.client
import os
import shutil
import signal
import socket
import ssl
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
def test_cli_serves_blackbull_app(tmp_path: Path, child_env):
    """``blackbull module:app`` resolves a BlackBull instance and serves it."""
    port = _free_port()
    script = tmp_path / 'cli_app.py'
    script.write_text(textwrap.dedent('''
        import blackbull, os, pathlib
        assert pathlib.Path(blackbull.__file__).resolve().is_relative_to(
            pathlib.Path(os.environ['BB_TEST_TREE_ROOT'])), blackbull.__file__
        from blackbull import BlackBull

        app = BlackBull()

        @app.route(path='/version')
        async def version():
            return b'cli-v1'
    ''').lstrip())

    env = child_env({'BB_ACCESS_LOG': '0', 'PYTHONUNBUFFERED': '1'}, cwd=str(tmp_path))
    blackbull = shutil.which('blackbull', path=env['PATH'])
    assert blackbull, 'the harness must provide the blackbull executable'

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


_CERTS = Path(__file__).resolve().parents[1]


def _unverified_context() -> ssl.SSLContext:
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    return context


def _alpn(port: int) -> str | None:
    """The protocol the server selects when offered ``h2`` and ``http/1.1``."""
    context = _unverified_context()
    context.set_alpn_protocols(['h2', 'http/1.1'])
    with socket.create_connection(('127.0.0.1', port), timeout=_REQ_TIMEOUT_SEC) as raw:
        with context.wrap_socket(raw) as tls:
            return tls.selected_alpn_protocol()


def _get_tls(port: int, path: str) -> bytes | None:
    try:
        conn = http.client.HTTPSConnection('127.0.0.1', port, timeout=_REQ_TIMEOUT_SEC,
                                           context=_unverified_context())
        conn.request('GET', path)
        body = conn.getresponse().read()
        conn.close()
        return body
    except (OSError, http.client.HTTPException):
        return None


@pytest.mark.parametrize('workers', ['1', '2'])
@pytest.mark.parametrize('form', ['app', 'serve'])
@pytest.mark.parametrize('source', ['flags', 'environment'])
@pytest.mark.timeout(60)
def test_a_tls_pair_makes_a_named_host_speak_only_https(tmp_path: Path, child_env,
                                                         source, form, workers):
    """A pair from the flags or BLACKBULL_CERT/KEY, a host in --bind: HTTPS there only."""
    port = _free_port()
    cert, key = str(_CERTS / 'cert.pem'), str(_CERTS / 'key.pem')
    pair = {'BLACKBULL_CERT': cert, 'BLACKBULL_KEY': key} if source == 'environment' else {}
    flags = ['--certfile', cert, '--keyfile', key] if source == 'flags' else []
    (tmp_path / 'tls_app.py').write_text(textwrap.dedent('''
        from blackbull import BlackBull
        app = BlackBull()
        @app.route(path='/version.txt')
        async def version():
            return b'tls-v1'
    ''').lstrip())
    (tmp_path / 'version.txt').write_bytes(b'tls-v1')
    env = child_env({'BB_ACCESS_LOG': '0', 'PYTHONUNBUFFERED': '1', **pair}, cwd=str(tmp_path))
    blackbull = shutil.which('blackbull', path=env['PATH'])
    assert blackbull, 'the harness must provide the blackbull executable'
    target = ['tls_app:app'] if form == 'app' else ['serve', str(tmp_path)]
    proc = subprocess.Popen(
        [blackbull, *target, '--bind', f'127.0.0.1:{port}', '--workers', workers, *flags],
        env=env, cwd=str(tmp_path), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, start_new_session=True,
    )
    try:
        body = _wait_until(lambda: _get_tls(port, '/version.txt'),
                           deadline=time.monotonic() + _STARTUP_DEADLINE_SEC)
        assert body == b'tls-v1'
        assert _alpn(port) == 'h2'
        assert _get(port, '/version.txt') is None
    finally:
        os.killpg(proc.pid, signal.SIGTERM)
        try:
            output = proc.communicate(timeout=15)[0]
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
            output = proc.communicate(timeout=5)[0]
        print(output[-4000:])


@pytest.mark.timeout(45)
def test_cli_serves_over_unix_domain_socket(tmp_path: Path, child_env):
    """``blackbull module:app --bind unix:/path`` serves traffic through AF_UNIX.

    Nginx → BlackBull deployments use UDS to avoid exposing
    a TCP port.  Verify the CLI parses the spec, ASGIServer binds AF_UNIX,
    and a client can complete a request through the socket file.
    """
    sock_path = tmp_path / 'bb.sock'
    script = tmp_path / 'uds_app.py'
    script.write_text(textwrap.dedent('''
        import blackbull, os, pathlib
        assert pathlib.Path(blackbull.__file__).resolve().is_relative_to(
            pathlib.Path(os.environ['BB_TEST_TREE_ROOT'])), blackbull.__file__
        from blackbull import BlackBull
        app = BlackBull()
        @app.route(path='/version')
        async def version():
            return b'uds-v1'
    ''').lstrip())

    env = child_env({'BB_ACCESS_LOG': '0', 'PYTHONUNBUFFERED': '1'}, cwd=str(tmp_path))
    blackbull = shutil.which('blackbull', path=env['PATH'])
    assert blackbull, 'the harness must provide the blackbull executable'

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
def test_cli_serves_raw_asgi_callable(tmp_path: Path, child_env):
    """``blackbull`` can serve a plain ASGI callable (no BlackBull instance).

    This is the path the benchmark harness takes —
    pointing the CLI at ``bench.apps.asgi_app:app`` (a raw ASGI app)
    just like every other peer server.
    """
    port = _free_port()
    script = tmp_path / 'raw_asgi_app.py'
    script.write_text(textwrap.dedent('''
        import blackbull, os, pathlib
        assert pathlib.Path(blackbull.__file__).resolve().is_relative_to(
            pathlib.Path(os.environ['BB_TEST_TREE_ROOT'])), blackbull.__file__
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

    env = child_env({'BB_ACCESS_LOG': '0', 'PYTHONUNBUFFERED': '1'}, cwd=str(tmp_path))
    blackbull = shutil.which('blackbull', path=env['PATH'])
    assert blackbull, 'the harness must provide the blackbull executable'

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
import asyncio, os, pathlib, signal, sys

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
        if marker := os.environ.get('PARK_STARTUP_MARKER'):
            pathlib.Path(marker).touch()
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


def _spawn_server(tmp_path: Path, body: str, port: int, child_env,
                  extra_env: dict | None = None) -> subprocess.Popen:
    (tmp_path / 'shutdown_app.py').write_text(
        _SHUTDOWN_APP.format(body=body, hold=_SLOW_SECONDS).lstrip())

    extra = dict(extra_env or {})
    py_path = str(tmp_path)
    if extra.get('PYTHONPATH'):
        py_path = os.pathsep.join([py_path, extra['PYTHONPATH']])
    env = child_env({'BB_ACCESS_LOG': '0', 'PYTHONUNBUFFERED': '1',
                     'PYTHONPATH': py_path, **extra}, cwd=str(tmp_path))

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
                       sig: int, child_env) -> subprocess.CompletedProcess:
    port = _free_port()
    proc = _spawn_server(tmp_path, body, port, child_env)
    proc.send_signal(sig)
    return _reap(proc)


@pytest.mark.parametrize('sig', [signal.SIGINT, signal.SIGTERM],
                         ids=['sigint', 'sigterm'])
@pytest.mark.timeout(60)
def test_a_failing_shutdown_hook_exits_non_zero(tmp_path: Path, sig: int, child_env):
    done = _serve_then_signal(
        tmp_path, "raise RuntimeError('flush to disk failed')", sig, child_env)
    assert 'SHUTDOWN_HOOK_RAN' in done.stderr, (
        f'the shutdown hook never ran on {signal.Signals(sig).name}\n'
        f'{done.stderr[-2000:]}')
    assert done.returncode == 1, (
        f'a failed shutdown exited {done.returncode}\n{done.stderr[-2000:]}')
    assert 'flush to disk failed' in done.stderr, done.stderr[-2000:]


@pytest.mark.parametrize('sig', [signal.SIGINT, signal.SIGTERM],
                         ids=['sigint', 'sigterm'])
@pytest.mark.timeout(60)
def test_a_clean_shutdown_hook_still_exits_zero(tmp_path: Path, sig: int, child_env):
    done = _serve_then_signal(tmp_path, 'return', sig, child_env)
    assert 'SHUTDOWN_HOOK_RAN' in done.stderr, (
        f'the shutdown hook never ran on {signal.Signals(sig).name}\n'
        f'{done.stderr[-2000:]}')
    assert done.returncode == 0, (
        f'a clean shutdown exited {done.returncode}\n{done.stderr[-2000:]}')


@pytest.mark.timeout(60)
def test_sigterm_lets_a_request_in_flight_finish(tmp_path: Path, child_env):
    port = _free_port()
    proc = _spawn_server(tmp_path, 'return', port, child_env)
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
def test_sigterm_during_a_parked_lifespan_startup_still_stops(tmp_path: Path, child_env):
    port = _free_port()
    marker = tmp_path / 'startup-parked'
    proc = _spawn_server(tmp_path, 'return', port, child_env,
                         {'PARK_STARTUP': '60', 'PARK_STARTUP_MARKER': str(marker)})
    if not _wait_until(marker.exists, deadline=time.monotonic() + _STARTUP_DEADLINE_SEC):
        proc.kill()
        done = _reap(proc)
        pytest.fail(f'the startup hook was never entered: {done.stderr[-2000:]}')
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
        tmp_path: Path, child_env):
    port = _free_port()
    proc = _spawn_server(tmp_path, 'return', port, child_env, {'OWN_SIGTERM_HANDLER': '1'})
    proc.send_signal(signal.SIGTERM)
    done, killed = _reap_or_kill(proc)
    assert not killed, done.stderr[-2000:]
    assert 'OWN_HANDLER_RAN' in done.stderr, (
        f"the application's own SIGTERM handler never ran\n{done.stderr[-2000:]}")
    assert done.returncode == 7, (
        f'the application chose 7; got {done.returncode}\n{done.stderr[-2000:]}')


@pytest.mark.timeout(90)
def test_the_shutdown_hook_runs_before_an_applications_sigterm_handler(
        tmp_path: Path, child_env):
    port = _free_port()
    proc = _spawn_server(tmp_path, 'return', port, child_env, {'OWN_SIGTERM_HANDLER': '1'})
    proc.send_signal(signal.SIGTERM)
    done, killed = _reap_or_kill(proc)
    assert not killed, done.stderr[-2000:]
    assert 'SHUTDOWN_HOOK_RAN' in done.stderr, done.stderr[-2000:]
    assert done.stderr.index('SHUTDOWN_HOOK_RAN') < done.stderr.index('OWN_HANDLER_RAN'), (
        "the application's handler ran before the shutdown it preempted\n"
        f'{done.stderr[-2000:]}')


@pytest.mark.timeout(90)
def test_two_sigterms_run_the_handler_twice_and_the_shutdown_once(tmp_path: Path, child_env):
    port = _free_port()
    proc = _spawn_server(tmp_path, 'return', port, child_env, {'OWN_SIGTERM_HANDLER': 'stay'})
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
