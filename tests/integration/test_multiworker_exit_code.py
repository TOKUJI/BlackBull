"""The master's exit code reflects how its workers ended (BLA-452)."""
from __future__ import annotations

import http.client
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

_APP = '''
import os

from blackbull import BlackBull

app = BlackBull()


@app.route(path='/ready')
async def ready():
    return b'ready'


@app.route(path='/pid')
async def pid():
    return str(os.getpid()).encode()


@app.route(path='/crash')
async def crash():
    os._exit(7)


@app.on_shutdown
async def _flush():
    {body}
'''

_CLI_ENTRY = 'from blackbull.cli import main; raise SystemExit(main())'


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


def _get(port: int, path: str) -> bytes | None:
    try:
        conn = http.client.HTTPConnection('127.0.0.1', port, timeout=1)
        conn.request('GET', path)
        body = conn.getresponse().read()
        conn.close()
        return body
    except OSError:
        return None


def _spawn_master(tmp_path: Path, body: str, port: int, child_env, *,
                  reload: bool = False):
    (tmp_path / 'app.py').write_text(_APP.format(body=body))
    argv = [sys.executable, '-c', _CLI_ENTRY, 'app:app',
            '--bind', f'127.0.0.1:{port}', '--workers', '2']
    if reload:
        argv.append('--reload')
    log_path = tmp_path / 'master.log'
    log_fh = open(log_path, 'w', buffering=1)
    proc = subprocess.Popen(
        argv, env=child_env({'WATCHFILES_FORCE_POLLING': '1',
                              'PYTHONUNBUFFERED': '1'}),
        cwd=str(tmp_path), stdout=log_fh, stderr=subprocess.STDOUT,
        text=True, start_new_session=True)
    log_fh.close()
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if _get(port, '/ready') == b'ready':
            return proc, log_path
        if proc.poll() is not None:
            raise AssertionError(
                f'master died before serving:\n{log_path.read_text()[-2000:]}')
        time.sleep(0.1)
    raise AssertionError('master never served /ready')


def _stop(proc: subprocess.Popen, log_path: Path) -> None:
    proc.send_signal(signal.SIGTERM)
    try:
        proc.wait(timeout=20)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)
        raise AssertionError(
            f'master did not stop:\n{log_path.read_text()[-2000:]}')


@pytest.mark.timeout(60)
def test_a_clean_shutdown_of_all_workers_exits_zero(tmp_path, child_env):
    port = _free_port()
    proc, log_path = _spawn_master(tmp_path, 'return', port, child_env)
    _stop(proc, log_path)
    assert proc.returncode == 0, log_path.read_text()[-2000:]


@pytest.mark.timeout(60)
def test_a_failed_worker_cleanup_fails_the_master(tmp_path, child_env):
    port = _free_port()
    proc, log_path = _spawn_master(
        tmp_path, "raise RuntimeError('flush to disk failed')", port, child_env)
    _stop(proc, log_path)
    assert proc.returncode != 0, log_path.read_text()[-2000:]
    assert 'flush to disk failed' in log_path.read_text()


@pytest.mark.timeout(60)
def test_a_respawned_worker_does_not_fail_the_master(tmp_path, child_env):
    port = _free_port()
    proc, log_path = _spawn_master(tmp_path, 'return', port, child_env)
    before = {_get(port, '/pid') for _ in range(20)}
    _get(port, '/crash')  # the worker taking it dies; the master respawns
    deadline = time.monotonic() + 20
    respawned = False
    while time.monotonic() < deadline and not respawned:
        respawned = _get(port, '/pid') not in before
        time.sleep(0.1)
    assert respawned, log_path.read_text()[-2000:]
    _stop(proc, log_path)
    assert proc.returncode == 0, log_path.read_text()[-2000:]


@pytest.mark.timeout(60)
def test_the_same_judgment_applies_under_reload(tmp_path, child_env):
    port = _free_port()
    proc, log_path = _spawn_master(
        tmp_path, "raise RuntimeError('flush to disk failed')", port,
        child_env, reload=True)
    _stop(proc, log_path)
    assert proc.returncode != 0, log_path.read_text()[-2000:]
