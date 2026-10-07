"""Master re-exec with inherited listening sockets.

Re-import code by replacing the master, not importlib.reload. Keep listeners
open across exec and close temporary event loops before fork.
"""
from __future__ import annotations

import logging
import os
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


#: How many changed paths a single log line names before it summarises.
#: One editor save is one path; a branch checkout is hundreds, and the
#: line has to stay readable in both cases.
_MAX_LOGGED_PATHS = 3


def _describe_changes(changes: Iterable[tuple[object, str]]) -> str:
    """Render one ``watchfiles`` batch as a bounded, stable path list."""
    paths = sorted({path for _, path in changes})
    head = ', '.join(paths[:_MAX_LOGGED_PATHS])
    hidden = len(paths) - _MAX_LOGGED_PATHS
    return f'{head} (+{hidden} more)' if hidden > 0 else head


def _default_filter(change, path: str) -> bool:  # noqa: ARG001
    """Accept only Python files for automatic reload.
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

        def _loop() -> None:
            try:
                # ``stop_event`` is the cooperative shutdown signal.
                # ``watch_filter`` selects which files we care about
                # (drops .pyc churn, dotfiles, editor swap files).
                for changes in watchfiles.watch(
                    *self._paths,
                    watch_filter=self._watch_filter,
                    stop_event=self._stop_event,
                ):
                    if self._stop_event.is_set():
                        return
                    # Log detection before invoking reload so an ignored callback is diagnosable.
                    logger.info('auto-reload: change detected in %s',
                                _describe_changes(changes))
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

    def stop(self, timeout: float = 2.0) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=max(0.0, timeout))
            if self._thread.is_alive():
                raise TimeoutError('file watcher did not stop before the deadline')
            self._thread = None


def exec_self_with_sockets(sockets: Sequence[socket.socket],
                           argv: Sequence[str] | None = None) -> None:
    """Re-execute without returning on success, preserving listening descriptors.

    The caller must terminate subprocesses holding copies first. argv defaults
    to sys.argv; sys.executable is always the executable.
    """
    if argv is None:
        argv = sys.argv
    if not argv:
        raise RuntimeError('cannot re-exec: sys.argv is empty')

    previous_env = os.environ.get(_INHERIT_FDS_ENV)
    previous_flags: list[tuple[int, bool]] = []
    try:
        fd_strings: list[str] = []
        for sock in sockets:
            fd = sock.fileno()
            if fd < 0:
                logger.warning('skipping closed socket during exec')
                continue
            previous_flags.append((fd, os.get_inheritable(fd)))
            os.set_inheritable(fd, True)
            fd_strings.append(str(fd))

        if not fd_strings:
            raise RuntimeError('exec_self_with_sockets: no live sockets to hand off')

        os.environ[_INHERIT_FDS_ENV] = ','.join(fd_strings)
        logger.info('reload: execv %s argv=%r BB_INHERIT_FDS=%s',
                    sys.executable, list(argv), os.environ[_INHERIT_FDS_ENV])

        # execvp replaces the process image — only returns on failure.
        os.execvp(sys.executable, [sys.executable, *argv])
        raise RuntimeError('execvp returned without replacing the process')
    finally:
        for fd, inheritable in previous_flags:
            try:
                os.set_inheritable(fd, inheritable)
            except OSError:
                logger.exception('Failed to restore inheritable flag for fd %d', fd)
        if previous_env is None:
            os.environ.pop(_INHERIT_FDS_ENV, None)
        else:
            os.environ[_INHERIT_FDS_ENV] = previous_env
