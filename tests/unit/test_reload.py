"""Unit tests for blackbull/server/reload.py and the adopt_inherited_sockets
helper in blackbull/protocol/rsock.py.

The full end-to-end reload flow (file change -> worker recycle ->
master re-exec -> new code served) lives in
tests/integration/test_reload.py because it requires a real subprocess.
This file covers the pieces that can be tested in-process.
"""
from __future__ import annotations

import os
import socket
import threading
import time
from pathlib import Path

import pytest

from blackbull.protocol.rsock import _INHERIT_FDS_ENV, adopt_inherited_sockets
from blackbull.server.reload import FileChangeWatcher, _default_filter


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

def test_default_filter_accepts_only_py():
    assert _default_filter(None, '/x/y/app.py') is True
    assert _default_filter(None, '/x/y/notes.md') is False
    assert _default_filter(None, '/x/y/__pycache__/app.cpython-312.pyc') is False


def test_watcher_fires_callback_on_py_change(tmp_path: Path):
    """Touching a .py file inside the watched dir must invoke the callback."""
    target = tmp_path / 'app.py'
    target.write_text('print("v1")\n')

    fired = threading.Event()

    def _on_change():
        fired.set()

    watcher = FileChangeWatcher([str(tmp_path)], _on_change)
    watcher.start()
    try:
        # Give the watchfiles thread time to install its inotify watch
        # before we start mutating files.  The first call also has to
        # load watchfiles' Rust extension, which is slower than steady-state.
        time.sleep(0.8)
        target.write_text('print("v2")\n')
        # watchfiles default debounce is ~50 ms; allow 2 s to be safe.
        assert fired.wait(timeout=2.0), (
            'FileChangeWatcher did not fire on .py edit within 2 s'
        )
    finally:
        watcher.stop()


def test_watcher_ignores_non_py(tmp_path: Path):
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

    # Pre-bind a master socket the way app.serve() does.
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
