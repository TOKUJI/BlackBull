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
import socket
import subprocess
import sys
import textwrap
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

    Sprint 12a — nginx → BlackBull deployments use UDS to avoid exposing
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

    This is the path the benchmark harness will take after Sprint 11 —
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
    # Sprint 80: BlackBull is a native-Connection framework — its server hands
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
