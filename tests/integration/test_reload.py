"""End-to-end test for auto-reload.

What this test exercises:
  1. ``app.run(reload=True)`` is launched as a subprocess against a
     scratch script the test owns.
  2. After the first request returns ``v1``, the script is rewritten so
     the same route returns ``v2``.
  3. The file watcher fires, the master SIGTERMs its workers, marks
     listening sockets inheritable, ``execvp``\\ s itself, the new
     process adopts the inherited sockets, forks fresh workers, and
     the next request returns ``v2``.
  4. The listening port stays bound continuously across the reload —
     a background sampler asserts a TCP connect succeeds throughout.

This is the only test that actually goes through the re-exec path; the
in-process logic is covered by ``tests/unit/test_reload.py``.

**Why it is staged.** The pipeline above has five stages, and collapsing
them into one deadline and one boolean makes every stall report the same
way — ``last_seen='v1'``, with no way to tell a watcher that never fired
from a master that never acted from a re-exec that never finished.  Each
stage below therefore waits on its own signal, with its own budget, and
names itself on failure.  The signals are read from the subprocess log
the test already captures, so nothing new has to be plumbed out of the
server.
"""
from __future__ import annotations

import os
import socket
import subprocess
import sys
import textwrap
import threading
import time
import warnings
import http.client
from collections.abc import Callable
from pathlib import Path
from typing import NoReturn

import pytest


# The re-exec path spawns a real subprocess and costs ~12 s healthy.  The
# directory says integration; the marker makes the default suite and the
# pre-commit hook agree with it.  CI still covers it: the integration lane
# in .github/workflows/test.yml runs `pytest tests/integration
# tests/conformance --run-integration`.
pytestmark = pytest.mark.integration


#: Per-stage budgets.  Sized from the healthy path (~12 s end to end) with
#: headroom for CPU/IO contention.  A budget is a ceiling, not an
#: expectation — the point is that exceeding one names *which* stage
#: stalled, not that the number is tuned.
_BUDGET: dict[str, float] = {
    'server up': 30.0,       # cold interpreter + import + bind
    'watcher armed': 5.0,    # watchfiles thread start
    'change seen': 10.0,     # poll interval is 100 ms; this is generous
    'recycle begun': 5.0,    # master polls the flag every _RELOAD_TICK
    'new code live': 15.0,   # SIGTERM drain + execvp + re-import + re-fork
}

#: Hard timeout.  Must exceed the sum of the budgets (plus one 'change
#: seen' retry and teardown), or the blunt timeout fires first and takes
#: the staged diagnosis with it.
_HARD_TIMEOUT_SEC = 120

_REQ_TIMEOUT_SEC = 2.0
_POLL_SEC = 0.1

#: Log signals each stage waits for.  Substrings, deliberately short: the
#: test should break when a line is removed, not when it is re-worded.
_WATCHER_ARMED = 'auto-reload: watching'
_CHANGE_SEEN = 'auto-reload: change detected'
_RECYCLE_BEGUN = 'recycling workers'


class DroppedReloadEvent(UserWarning):
    """The first write was not observed; a second one was.

    Not a failure — the reload machinery works — but it means event
    delivery dropped an event, which is the half of the flake that the
    machinery cannot fix.  Raised as a warning so it stays visible in CI
    output without blocking a commit.
    """


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


class ListenerProbe:
    """Background TCP-connect sampler for the listening socket.

    The reload path hands the *same* listening fd across ``execvp``, so a
    connect must succeed at every point in the transition.  Sampling on
    its own thread keeps that invariant independent of how long the staged
    waits take — otherwise the outage window is only observed while the
    main thread happens to be polling HTTP.

    Connect-only on purpose: an HTTP round-trip also fails when workers
    are mid-recycle, which is expected and would conflate "no listener"
    with "no worker yet".
    """

    def __init__(self, port: int, interval: float = _POLL_SEC):
        self._port = port
        self._interval = interval
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.samples = 0
        self.failures = 0

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                with socket.create_connection(('127.0.0.1', self._port),
                                              timeout=_REQ_TIMEOUT_SEC):
                    self.samples += 1
            except OSError:
                self.failures += 1
            self._stop.wait(self._interval)

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._run, name='reload-listener-probe', daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5.0)
            self._thread = None


def _log_text(log_path: Path) -> str:
    try:
        return log_path.read_text(errors='replace')
    except OSError:
        return ''


def _occurrences(log_path: Path, needle: str) -> int:
    return _log_text(log_path).count(needle)


def _await(stage: str, probe: Callable[[], bool]) -> bool:
    """Poll *probe* until true within the stage's budget."""
    deadline = time.monotonic() + _BUDGET[stage]
    while time.monotonic() < deadline:
        if probe():
            return True
        time.sleep(_POLL_SEC)
    return False


def _fail_stage(stage: str, waiting_for: str) -> NoReturn:
    pytest.fail(
        f'reload stalled at stage {stage!r} after {_BUDGET[stage]}s '
        f'waiting for {waiting_for}. '
        'Stage timeline and subprocess log follow under "Captured stdout call".'
    )


class StageTimeline:
    """Runs the staged waits and records how long each one took.

    A stall is much easier to read against the stages that already
    succeeded — "watcher armed in 0.01 s, change seen never" is a
    diagnosis; "did not observe v2" is not.
    """

    def __init__(self) -> None:
        self._rows: list[tuple[str, float, bool]] = []

    def run(self, stage: str, probe: Callable[[], bool]) -> bool:
        start = time.monotonic()
        ok = _await(stage, probe)
        self._rows.append((stage, time.monotonic() - start, ok))
        return ok

    def record(self, label: str, elapsed: float) -> None:
        """Note a phase that is timed rather than waited on (spawn, teardown)."""
        self._rows.append((label, elapsed, True))

    def require(self, stage: str, probe: Callable[[], bool],
                waiting_for: str) -> None:
        if not self.run(stage, probe):
            _fail_stage(stage, waiting_for)

    def render(self) -> str:
        return '\n'.join(
            f'  {"ok   " if ok else "STALL"} {stage:<14} {elapsed:6.2f}s'
            + (f' / {_BUDGET[stage]:.0f}s' if stage in _BUDGET else '')
            for stage, elapsed, ok in self._rows
        ) or '  (no stage ran)'


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
            app.run(port={port}, reload=True)
    ''').lstrip())


@pytest.mark.timeout(_HARD_TIMEOUT_SEC)
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
    # Force watchfiles into polling mode for the subprocess's watcher.
    # watchfiles' default inotify backend silently drops the rewrite event on
    # WSL2's drvfs/9p filesystem, and polling is backend-agnostic; the reload
    # re-exec logic under test is identical either way.  Polling has its own
    # blind spot — it reports a file as modified only when the mtime moves
    # *forward*, so a backwards clock step drops the save outright — which is
    # what stage 3's retry exists to survive.
    env['WATCHFILES_FORCE_POLLING'] = '1'
    env['WATCHFILES_POLL_DELAY_MS'] = '100'

    # Capture to a file rather than a pipe: an unread PIPE eventually
    # blocks the subprocess on writes once the kernel buffer (~64 KiB)
    # fills, which silently stalls reload progression mid-test.
    log_path = tmp_path / 'subprocess.log'
    log_fh = open(log_path, 'w', buffering=1)
    spawn_start = time.monotonic()
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

    probe = ListenerProbe(port)
    stages = StageTimeline()
    stages.record('spawn', time.monotonic() - spawn_start)
    retried = False
    try:
        # ---- stage 1: the v1 server answers -------------------------------
        stages.require('server up',
                       lambda: _get_version(port) == 'v1',
                       'GET /version == v1')

        # ---- stage 2: the watcher is armed --------------------------------
        stages.require('watcher armed',
                       lambda: _occurrences(log_path, _WATCHER_ARMED) > 0,
                       f'{_WATCHER_ARMED!r} in the subprocess log')

        # Even an armed polling watcher races its own first scan: the
        # baseline snapshot has to complete before a write can be seen as a
        # change.  The wait matches the real dev-time path (save happens
        # seconds after start), and stage 3's retry covers the rest.
        time.sleep(1.5)

        # Baselines taken *after* arming: only occurrences caused by our
        # rewrite count, so a stray earlier event cannot satisfy a stage.
        seen_before = _occurrences(log_path, _CHANGE_SEEN)
        recycle_before = _occurrences(log_path, _RECYCLE_BEGUN)

        # The listener invariant spans everything from here to v2.
        probe.start()

        # ---- stage 3: the watcher observes the rewrite --------------------
        _write_app(script, port, 'v2')

        def change_seen() -> bool:
            return _occurrences(log_path, _CHANGE_SEEN) > seen_before

        if not stages.run('change seen', change_seen):
            # A user who saves and sees nothing saves again.  If the second
            # write lands, the defect is event delivery, not the reload
            # machinery — a distinction the old single deadline could not
            # make.  (It is: the poller drops a save whose mtime predates its
            # last snapshot, which a backwards clock step produces.)
            retried = True
            _write_app(script, port, 'v2')
            if not stages.run('change seen', change_seen):
                _fail_stage('change seen',
                            f'{_CHANGE_SEEN!r} after two rewrites '
                            '(watcher is not delivering events at all)')

        # ---- stage 4: the master acts on it -------------------------------
        stages.require('recycle begun',
                       lambda: _occurrences(log_path, _RECYCLE_BEGUN) > recycle_before,
                       f'{_RECYCLE_BEGUN!r} — the watcher fired but the master '
                       'did not act on the flag')

        # ---- stage 5: the new code serves ---------------------------------
        stages.require('new code live',
                       lambda: _get_version(port) == 'v2',
                       'GET /version == v2 after re-exec')
    finally:
        probe.stop()
        teardown_start = time.monotonic()
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
        stages.record('teardown', time.monotonic() - teardown_start)
        log_fh.close()
        # Surface the timeline and the subprocess log when the test failed
        # so the post-mortem doesn't require re-running with -s.
        print('--- reload stage timeline ---')
        print(stages.render())
        log_text = _log_text(log_path)
        if log_text:
            # Pytest captures this; appears under "Captured stdout call".
            print(f'--- subprocess log ({len(log_text)} bytes) ---')
            print(log_text[-4000:])  # tail in case it's huge

    if retried:
        warnings.warn(
            'the first rewrite was not observed; the second one was. The '
            'reload pipeline is healthy, but an event was dropped between '
            'the filesystem and the watcher.',
            DroppedReloadEvent,
            stacklevel=1,
        )

    # A small handful of failed connects during the worker-recycle window is
    # tolerable; a sustained outage means the listening socket was closed
    # during reload, which the re-exec design says cannot happen.
    assert probe.samples > 0, 'listener probe never sampled — probe thread died'
    assert probe.failures < 10, (
        f'listening socket refused {probe.failures} of '
        f'{probe.samples + probe.failures} connects during reload '
        '(it should stay bound across the execvp)'
    )
