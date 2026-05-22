"""Auto-reload support for the BlackBull multi-worker master.

The reload model is **master re-exec**:

  1. Master binds and listens on the configured sockets.
  2. Master forks worker processes; they inherit the listening sockets
     via fd inheritance.
  3. A background ``watchfiles`` watcher signals the master when any
     watched ``*.py`` file changes.
  4. Master sends SIGTERM to all workers and waits up to
     ``shutdown_timeout`` for them to drain in-flight requests.
  5. Master marks the listening sockets inheritable, exports their fds
     via ``BB_INHERIT_FDS``, and ``os.execvp``\\ s ``sys.executable``
     with the original argv.
  6. The fresh master process adopts the inherited sockets
     (see :func:`blackbull.protocol.rsock.adopt_inherited_sockets`)
     and re-forks workers — now running the *new* code.

Picking up new code requires the master itself to re-import, which is
why we re-exec the whole process rather than ``importlib.reload``.  The
listening sockets do not close at any point: kernel multiplexes the
same fd across master+workers; only python state churns.

The watcher runs in a daemon thread so it can not block the master's
synchronous supervision loop.  It debounces filesystem events itself
(watchfiles default ~50 ms) so a single editor save does not trigger
multiple reloads.
"""
from __future__ import annotations

import logging
import os
import signal
import socket
import sys
import threading
from collections.abc import Callable, Iterable, Sequence
from pathlib import Path

logger = logging.getLogger(__name__)

_INHERIT_FDS_ENV = 'BB_INHERIT_FDS'

#: Default extensions watched when the user does not specify ``include``.
#: Templates and config files are intentionally NOT watched by default —
#: most edits to those do not require a process restart.
_DEFAULT_WATCH_SUFFIXES = ('.py',)


def _default_filter(change, path: str) -> bool:  # noqa: ARG001
    """watchfiles ``watch_filter`` accepting only ``*.py`` files.

    Lives outside the class so reuse from tests is trivial.  ``change``
    is a ``watchfiles.Change`` enum but we only care about path here.
    """
    return path.endswith(_DEFAULT_WATCH_SUFFIXES)


class FileChangeWatcher:
    """Daemon-thread wrapper around ``watchfiles.watch``.

    Parameters
    ----------
    paths:
        Iterable of filesystem paths to watch.  Each path can be a file
        or a directory; directories are watched recursively.
    on_change:
        Zero-argument callable invoked exactly once per debounced batch
        of file events.  Runs on the watcher thread, so must be cheap
        and threadsafe — typical use is to set a ``threading.Event``
        the master's main loop polls.
    watch_filter:
        Optional ``watchfiles`` ``watch_filter`` callable.  Defaults to
        ``*.py``-only.
    """

    def __init__(self, paths: Iterable[str | Path],
                 on_change: Callable[[], None],
                 watch_filter: Callable[..., bool] | None = None):
        self._paths = [str(Path(p).resolve()) for p in paths]
        self._on_change = on_change
        self._watch_filter = watch_filter or _default_filter
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        """Start the watcher thread.  No-op if already running."""
        if self._thread is not None and self._thread.is_alive():
            return

        try:
            import watchfiles  # noqa: PLC0415
        except ImportError as exc:
            raise RuntimeError(
                "auto-reload requires 'watchfiles'. "
                "Install with: pip install -e '.[reload]'"
            ) from exc

        self._watchfiles = watchfiles

        def _loop() -> None:
            try:
                # ``stop_event`` is the cooperative shutdown signal.
                # ``watch_filter`` selects which files we care about
                # (drops .pyc churn, dotfiles, editor swap files).
                for _changes in watchfiles.watch(
                    *self._paths,
                    watch_filter=self._watch_filter,
                    stop_event=self._stop_event,
                ):
                    if self._stop_event.is_set():
                        return
                    try:
                        self._on_change()
                    except Exception:
                        logger.exception('reload on_change callback failed')
            except Exception:
                # If the watcher itself crashes we want to know, but not
                # take the whole server down — the master's primary job
                # is still to supervise workers.
                logger.exception('file watcher crashed; auto-reload disabled')

        self._thread = threading.Thread(
            target=_loop, name='bb-reload-watcher', daemon=True,
        )
        self._thread.start()
        logger.info('auto-reload: watching %s for *.py changes',
                    ', '.join(self._paths))

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None


def exec_self_with_sockets(sockets: Sequence[socket.socket],
                           argv: Sequence[str] | None = None) -> None:
    """Re-execute the current Python process, preserving listening sockets.

    Marks each socket's fd inheritable, sets ``BB_INHERIT_FDS`` to a
    comma-separated list of those fds, and calls ``os.execvp`` —
    which replaces the current process image while keeping fds open
    (unless ``FD_CLOEXEC`` is set, which we clear here).

    Does not return on success: the current process image is gone.

    Parameters
    ----------
    sockets:
        Listening sockets the new process should adopt.  Caller is
        responsible for having already terminated any subprocesses
        that hold copies of these fds.
    argv:
        Argv to exec.  Defaults to ``sys.argv`` (re-runs the same
        command line).  ``sys.executable`` is always used as argv[0].
    """
    if argv is None:
        argv = sys.argv
    if not argv:
        raise RuntimeError('cannot re-exec: sys.argv is empty')

    fd_strings: list[str] = []
    for sock in sockets:
        fd = sock.fileno()
        if fd < 0:
            logger.warning('skipping closed socket during exec')
            continue
        os.set_inheritable(fd, True)
        fd_strings.append(str(fd))

    if not fd_strings:
        raise RuntimeError('exec_self_with_sockets: no live sockets to hand off')

    os.environ[_INHERIT_FDS_ENV] = ','.join(fd_strings)
    logger.info('reload: execv %s argv=%r BB_INHERIT_FDS=%s',
                sys.executable, list(argv), os.environ[_INHERIT_FDS_ENV])

    # execvp replaces the process image — only returns on failure.
    os.execvp(sys.executable, [sys.executable, *argv])
