"""Unit tests for blackbull/server/reload.py and the adopt_inherited_sockets
helper in blackbull/protocol/rsock.py.

The full end-to-end reload flow (file change -> worker recycle ->
master re-exec -> new code served) lives in
tests/integration/test_reload.py because it requires a real subprocess.
This file covers the pieces that can be tested in-process.
"""
from __future__ import annotations

import logging
import os
import socket
import threading
import time
from pathlib import Path

import pytest

from blackbull.protocol.rsock import _INHERIT_FDS_ENV, adopt_inherited_sockets
from blackbull.server.reload import (
    _MAX_LOGGED_PATHS,
    FileChangeWatcher,
    _default_filter,
    _describe_changes,
)


# ---------------------------------------------------------------------------
# adopt_inherited_sockets
# ---------------------------------------------------------------------------

def test_adopt_returns_none_when_env_unset(monkeypatch):
    monkeypatch.delenv(_INHERIT_FDS_ENV, raising=False)
    assert adopt_inherited_sockets() is None


def test_adopt_returns_none_when_env_blank(monkeypatch):
    monkeypatch.setenv(_INHERIT_FDS_ENV, '')
    assert adopt_inherited_sockets() is None


def test_adopt_returns_none_when_env_malformed(monkeypatch):
    monkeypatch.setenv(_INHERIT_FDS_ENV, 'not-a-number,bogus')
    assert adopt_inherited_sockets() is None


def test_adopt_builds_sockets_and_clears_env(monkeypatch):
    """A pre-bound listening socket is adopted, env var is cleared."""
    src = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    src.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    src.bind(('127.0.0.1', 0))
    src.listen(8)
    port = src.getsockname()[1]
    # set_inheritable: in real reload flow exec_self_with_sockets does this.
    os.set_inheritable(src.fileno(), True)

    monkeypatch.setenv(_INHERIT_FDS_ENV, str(src.fileno()))
    adopted = adopt_inherited_sockets()
    try:
        assert adopted is not None
        assert len(adopted) == 1
        assert adopted[0].getsockname()[1] == port
        # env must be wiped so workers do not try to re-adopt.
        assert _INHERIT_FDS_ENV not in os.environ
    finally:
        for s in adopted or []:
            s.close()
        # `src` and `adopted[0]` share the same fd — closing both would
        # double-free.  Closing one is enough; do not also src.close().


# ---------------------------------------------------------------------------
# FileChangeWatcher
# ---------------------------------------------------------------------------

@pytest.fixture
def force_polling_watcher(monkeypatch):
    """Force watchfiles into polling mode for the in-process watcher tests.

    ``FileChangeWatcher`` uses watchfiles' default backend (inotify on
    Linux).  inotify has a startup window where the arming races with the
    first write, and on WSL2's drvfs/9p — and under the CPU/IO contention
    of the full test suite — it silently drops that first event.  That is
    the exact flake ``tests/integration/test_reload.py`` fixed by forcing
    polling in its subprocess env; these unit tests run the watcher
    in-process and were still on the flaky inotify path.

    watchfiles reads ``WATCHFILES_FORCE_POLLING`` at ``watch()`` time (in
    its Rust thread), so setting it before ``watcher.start()`` is enough.
    Polling is backend-agnostic and reliable across filesystems; the
    watcher logic under test is identical either way.  ``monkeypatch``
    restores the environment after the test.
    """
    monkeypatch.setenv('WATCHFILES_FORCE_POLLING', '1')
    monkeypatch.setenv('WATCHFILES_POLL_DELAY_MS', '50')


def test_default_filter_accepts_only_py():
    assert _default_filter(None, '/x/y/app.py') is True
    assert _default_filter(None, '/x/y/notes.md') is False
    assert _default_filter(None, '/x/y/__pycache__/app.cpython-312.pyc') is False


def test_watcher_fires_callback_on_py_change(tmp_path: Path, force_polling_watcher):
    """Touching a .py file inside the watched dir must invoke the callback.

    Robustness note: the ``force_polling_watcher`` fixture pins watchfiles
    to polling so the first write is not lost to an inotify startup race.
    We still keep rewriting the file at a slow cadence until the callback
    fires or the deadline expires — the real reload path has the same
    shape (user mashes Save until they see effect), and it costs nothing
    when the very first poll already catches the change.
    """
    target = tmp_path / 'app.py'
    target.write_text('print("v1")\n')

    fired = threading.Event()
    watcher = FileChangeWatcher([str(tmp_path)], fired.set)
    watcher.start()
    try:
        deadline = time.monotonic() + 5.0
        i = 2
        while time.monotonic() < deadline:
            target.write_text(f'print("v{i}")\n')
            if fired.wait(timeout=0.4):
                return
            i += 1
        pytest.fail('FileChangeWatcher did not fire on .py edit within 5 s')
    finally:
        watcher.stop()


def test_watcher_ignores_non_py(tmp_path: Path, force_polling_watcher):
    """Editing a .md file must NOT fire the callback (default filter)."""
    py = tmp_path / 'app.py'
    py.write_text('print("v1")\n')
    md = tmp_path / 'NOTES.md'
    md.write_text('hi\n')

    fired = threading.Event()
    watcher = FileChangeWatcher([str(tmp_path)], fired.set)
    watcher.start()
    try:
        time.sleep(0.3)
        md.write_text('there\n')
        # 0.5 s is enough — if it were going to fire, it would by then.
        assert not fired.wait(timeout=0.5), (
            'callback fired on .md edit; default filter should reject it'
        )
    finally:
        watcher.stop()


def test_watcher_logs_the_changed_path_before_firing(
    tmp_path: Path, force_polling_watcher, caplog,
):
    """The watcher must name what it saw, *before* handing off to the master.

    Without this line the only evidence a change was observed is the
    master's own "recycling workers", one tick later — so a reload that
    never happens cannot be attributed to the watcher or to the master.
    Ordering is part of the contract: the record has to be on disk before
    the callback runs, or a callback that re-execs takes the evidence with
    it.
    """
    caplog.set_level(logging.INFO, logger='blackbull.server.reload')
    target = tmp_path / 'app.py'
    target.write_text('print("v1")\n')

    logged_before_callback: list[bool] = []
    fired = threading.Event()

    def _on_change() -> None:
        # Scoped to our own logger: watchfiles emits its own DEBUG
        # "N change detected" line, which would satisfy a bare substring
        # match and make this assertion vacuous.
        logged_before_callback.append(
            any('change detected' in r.getMessage() for r in caplog.records
                if r.name == 'blackbull.server.reload')
        )
        fired.set()

    watcher = FileChangeWatcher([str(tmp_path)], _on_change)
    watcher.start()
    try:
        deadline = time.monotonic() + 5.0
        i = 2
        while time.monotonic() < deadline and not fired.is_set():
            target.write_text(f'print("v{i}")\n')
            fired.wait(timeout=0.4)
            i += 1
        assert fired.is_set(), 'watcher never fired; cannot assert on its log'
    finally:
        watcher.stop()

    assert logged_before_callback[0], (
        'callback ran before the change was logged — a re-exec in the '
        'callback would destroy the only record that the watcher saw it'
    )
    messages = [
        r.getMessage() for r in caplog.records
        if r.name == 'blackbull.server.reload'
    ]
    assert any(str(target) in m for m in messages), (
        f'no log record named the changed path {target}; got {messages}'
    )


def test_describe_changes_bounds_a_large_batch():
    """A branch checkout is one batch of hundreds of paths — the line stays readable."""
    changes = {(None, f'/x/f{i}.py') for i in range(50)}
    rendered = _describe_changes(changes)

    assert rendered.count(',') == _MAX_LOGGED_PATHS - 1
    assert rendered.endswith(f'(+{50 - _MAX_LOGGED_PATHS} more)')
    # Sorted, so the same batch always renders identically.
    assert _describe_changes(changes) == rendered


def test_describe_changes_names_a_single_file_exactly():
    assert _describe_changes({(None, '/x/app.py')}) == '/x/app.py'


def test_watcher_stop_is_idempotent(tmp_path: Path):
    watcher = FileChangeWatcher([str(tmp_path)], lambda: None)
    watcher.start()
    watcher.stop()
    watcher.stop()  # must not raise


# ---------------------------------------------------------------------------
# MultiWorkerServer __init__: reload disables REUSEPORT
# ---------------------------------------------------------------------------

def test_reload_disables_reuseport(monkeypatch):
    """With reload=True the master must hold the listening sockets (no
    SO_REUSEPORT per-worker close+rebind), even with workers>1."""
    from blackbull.server.multiworker import MultiWorkerServer
    from blackbull import BlackBull

    # Pre-bind a master socket the way bench/aws/run.sh does.
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(('127.0.0.1', 0))
    sock.listen(8)

    app = BlackBull()
    mws = MultiWorkerServer(app, [sock], None, workers=2, reload=True)
    try:
        # Master must still hold the listening socket so it can hand it
        # off across exec.  Worker socket sets must reference the same
        # master sockets (shared, not per-worker rebound).
        assert mws._listening_sockets == [sock]
        assert mws._worker_sockets[0] == [sock]
        assert mws._worker_sockets[1] == [sock]
    finally:
        sock.close()
