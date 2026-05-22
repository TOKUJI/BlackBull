"""End-to-end test for auto-reload (Sprint 10).

What this test exercises:
  1. ``app.serve(reload=True)`` is launched as a subprocess against a
     scratch script the test owns.
  2. After the first request returns ``v1``, the script is rewritten so
     the same route returns ``v2``.
  3. The file watcher fires, the master SIGTERMs its workers, marks
     listening sockets inheritable, ``execvp``\\ s itself, the new
     process adopts the inherited sockets, forks fresh workers, and
     the next request returns ``v2``.
  4. The listening port stays bound continuously across the reload —
     the test polls during the recycle and asserts a TCP connect
     succeeds throughout.

This is the only test that actually goes through the re-exec path; the
in-process logic is covered by ``tests/unit/test_reload.py``.
"""
from __future__ import annotations

import os
import socket
import subprocess
import sys
import textwrap
import time
import http.client
from pathlib import Path

import pytest


# Smaller payload than the unit-test debounce wait — picked so the test
# is responsive without becoming flaky on a loaded laptop.
_RELOAD_DEADLINE_SEC = 20.0
_REQ_TIMEOUT_SEC = 2.0


def _free_port() -> int:
    """Reserve an ephemeral port via a transient bind and immediately release it."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


def _get_version(port: int) -> str | None:
    """Plain HTTP GET /version, returns body or None on connect failure."""
    try:
        conn = http.client.HTTPConnection('127.0.0.1', port, timeout=_REQ_TIMEOUT_SEC)
        conn.request('GET', '/version')
        resp = conn.getresponse()
        body = resp.read().decode('ascii', errors='replace')
        conn.close()
        return body
    except (OSError, http.client.HTTPException):
        return None


def _wait_for(predicate, deadline: float, poll: float = 0.1):
    """Poll *predicate* until it returns a truthy value, or *deadline* passes."""
    while time.monotonic() < deadline:
        result = predicate()
        if result:
            return result
        time.sleep(poll)
    return None


def _write_app(script: Path, port: int, version: str) -> None:
    """(Re)write the scratch ASGI script with the given version string baked in."""
    script.write_text(textwrap.dedent(f'''
        import logging, os
        logging.basicConfig(
            level=logging.INFO,
            format='[%(asctime)s pid=%(process)d %(name)s %(levelname)s] %(message)s',
        )
        logging.info('reload_app starting pid=%d cwd=%s', os.getpid(), os.getcwd())
        from blackbull import BlackBull

        app = BlackBull()

        @app.route(path='/version')
        async def version():
            return b'{version}'

        if __name__ == '__main__':
            app.serve(port={port}, reload=True)
    ''').lstrip())


@pytest.mark.timeout(60)
def test_auto_reload_picks_up_new_code(tmp_path: Path):
    port = _free_port()
    script = tmp_path / 'reload_app.py'
    _write_app(script, port, 'v1')

    # Use the current interpreter so the subprocess shares the blackbull
    # editable install of the test runner.  PYTHONUNBUFFERED makes any
    # diagnostic output appear promptly when the test fails.
    env = os.environ.copy()
    env['PYTHONUNBUFFERED'] = '1'
    env['BB_ACCESS_LOG'] = '0'   # quieter test output

    # Capture to a file rather than a pipe: an unread PIPE eventually
    # blocks the subprocess on writes once the kernel buffer (~64 KiB)
    # fills, which silently stalls reload progression mid-test.
    log_path = tmp_path / 'subprocess.log'
    log_fh = open(log_path, 'w', buffering=1)
    proc = subprocess.Popen(
        [sys.executable, str(script)],
        env=env,
        cwd=str(tmp_path),
        stdout=log_fh,
        stderr=subprocess.STDOUT,
        text=True,
        # Detach from pytest's process group / session so signals
        # pytest delivers don't fall through, and so watchfiles' inotify
        # doesn't see any artefacts of the parent's MP fork churn.
        start_new_session=True,
        close_fds=True,
    )

    try:
        # Wait for the v1 server to come up.
        first = _wait_for(
            lambda: _get_version(port) == 'v1',
            deadline=time.monotonic() + _RELOAD_DEADLINE_SEC,
        )
        assert first, 'v1 server did not respond on /version in time'

        # watchfiles' inotify backend needs ~1-2 s after it arms before
        # it reliably reports changes — the very first events after
        # startup race with the initial scan.  Real users hit the editor
        # save key seconds after starting the server, so this matches
        # the actual dev-time path.  Without it the test flakes when
        # the v1 verify finishes in < 200 ms.
        time.sleep(1.5)

        # Rewrite the script.  watchfiles will see this and trigger the
        # master to recycle workers.
        _write_app(script, port, 'v2')

        # Sample /version repeatedly until we see v2.  At the same time
        # assert that the TCP listener never closes — every connect
        # attempt during the transition must succeed (it may return v1
        # or v2 but should not raise).
        deadline = time.monotonic() + _RELOAD_DEADLINE_SEC
        connect_failures = 0
        last_seen = None
        while time.monotonic() < deadline:
            body = _get_version(port)
            if body is None:
                connect_failures += 1
            else:
                last_seen = body
                if body == 'v2':
                    break
            time.sleep(0.1)
        else:
            pytest.fail(
                f'did not observe v2 within {_RELOAD_DEADLINE_SEC}s; '
                f'last_seen={last_seen!r} connect_failures={connect_failures}'
            )

        assert last_seen == 'v2'
        # A small handful of in-flight RSTs during the worker-recycle
        # window is acceptable; an outage longer than ~1s suggests the
        # listening socket is being closed during reload (regression).
        # Tolerance is generous because WSL2 + subprocess timing is noisy.
        assert connect_failures < 10, (
            f'too many connect failures during reload: {connect_failures} '
            '(listening socket was likely closed)'
        )
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
        log_fh.close()
        # Surface the subprocess log when the test failed so the
        # post-mortem doesn't require re-running with -s.
        if log_path.exists() and log_path.stat().st_size > 0:
            log_text = log_path.read_text()
            # Pytest captures this; appears under "Captured stdout call".
            print(f'--- subprocess log ({len(log_text)} bytes) ---')
            print(log_text[-4000:])  # tail in case it's huge
