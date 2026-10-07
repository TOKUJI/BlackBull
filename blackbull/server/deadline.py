"""Loop-scoped shared deadline scanner.

Enforcement can lag by one BB_DEADLINE_TICK_MS interval. The registry must
quiesce when empty and resume on the next arm; each worker owns its scanner.
"""
from __future__ import annotations

import asyncio
import os
from typing import ClassVar


_TICK_S: float = max(0.01,
                     float(os.environ.get('BB_DEADLINE_TICK_MS', '300')) / 1000.0)
_INF = float('inf')


class _Scanner:
    """Per-process scanner state; keep at most one tick handle armed.
    """

    _LOOP: ClassVar[asyncio.AbstractEventLoop | None] = None
    _HANDLE: ClassVar[asyncio.TimerHandle | None] = None
    _REGISTRY: ClassVar[set] = set()


def _tick() -> None:
    """Scanner callback.  Walks the registry, fires expired deadlines,
    re-arms unless the registry is empty."""
    loop = _Scanner._LOOP
    if loop is None:
        _Scanner._HANDLE = None
        return
    now = loop.time()
    # Snapshot — _fire_from_scanner mutates _REGISTRY.
    for dl in list(_Scanner._REGISTRY):
        if dl._deadline_at <= now:
            dl._fire_from_scanner()
    if _Scanner._REGISTRY:
        _Scanner._HANDLE = loop.call_later(_TICK_S, _tick)
    else:
        _Scanner._HANDLE = None


def _ensure_scanner_running(loop: 'asyncio.AbstractEventLoop') -> None:
    if _Scanner._LOOP is not loop:
        # Fresh loop (worker fork, test isolation, etc.) — drop the
        # stale handle and registry; the new loop's deadlines will
        # re-register on arm.
        _Scanner._LOOP = loop
        _Scanner._HANDLE = None
        _Scanner._REGISTRY = set()
    if _Scanner._HANDLE is None:
        _Scanner._HANDLE = loop.call_later(_TICK_S, _tick)


class ConnectionDeadline:
    """One reusable deadline per connection.

    The instance binds to the task that constructed it (in practice, the
    connection actor's task).  When the per-process scanner observes
    that the deadline's monotonic ``_deadline_at`` has passed, the bound
    task is cancelled — the cancellation propagates into whichever
    ``reader.readuntil`` / ``read`` / ``readexactly`` is currently
    awaiting.  Call sites translate the cancellation into
    ``TimeoutError`` via [`guard`][] (the common case) or manually
    by checking ``fired``.
    """

    __slots__ = ('_loop', '_task', '_deadline_at', '_fired',
                 '_pending', '_registered')

    def __init__(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._task = asyncio.current_task()
        self._deadline_at: float = _INF
        self._fired = False
        self._pending = 0.0
        self._registered = False

    def arm(self, seconds: float) -> None:
        """(Re-)set the deadline; ``seconds <= 0`` disables it.

        Safe to call repeatedly.  Resets ``fired`` so a recovered
        deadline can be reused across phases on the same connection.
        """
        self._fired = False
        if seconds > 0:
            self._deadline_at = self._loop.time() + seconds
            if not self._registered:
                _ensure_scanner_running(self._loop)
                _Scanner._REGISTRY.add(self)
                self._registered = True
        else:
            self._deadline_at = _INF
            if self._registered:
                _Scanner._REGISTRY.discard(self)
                self._registered = False

    def disarm(self) -> None:
        """Drop the deadline.  Idempotent."""
        self._deadline_at = _INF
        if self._registered:
            _Scanner._REGISTRY.discard(self)
            self._registered = False

    def _fire_from_scanner(self) -> None:
        """Invoked by [`_tick`][] when ``_deadline_at`` has passed."""
        self._fired = True
        self._deadline_at = _INF
        self._registered = False
        _Scanner._REGISTRY.discard(self)
        if self._task is not None and not self._task.done():
            self._task.cancel()

    @property
    def fired(self) -> bool:
        return self._fired

    def guard(self, seconds: float) -> 'ConnectionDeadline':
        """Arm the deadline and return ``self`` as a context manager.

        Caller pattern::

            with dl.guard(cfg.header_timeout):
                await reader.readuntil(...)

        Matches the observable behaviour of ``async with asyncio.timeout(d):``
        — a fired deadline manifests as ``TimeoutError``, and a read that
        completes in the same tick the deadline fires is a timeout, which is
        ``asyncio.timeout``'s convention too.

        ``self`` *is* the context manager, so guards cannot nest or overlap:
        each connection owns one of these and uses it sequentially from the
        single task that constructed it.
        """
        self._pending = seconds
        return self

    def __enter__(self) -> 'ConnectionDeadline':
        self.arm(self._pending)
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        self.disarm()
        if self._fired and (exc_type is None or exc_type is asyncio.CancelledError):
            task = asyncio.current_task()
            if task is not None:
                # Clear the cancel that ``_fire_from_scanner`` requested;
                # the read either raised CancelledError (suppressed here)
                # or completed normally in the same tick the scanner
                # fired.  Either way the surrounding task must not stay
                # in cancelled state.
                task.uncancel()
            raise TimeoutError
        return False


class WriteDeadline:
    """Bound writer drain with a task owner selected per arm.

    Concurrent riders must not rearm or interpret firing. The owner closes the
    transport on deadline, resolving every rider; one shared scanner serves all.
    """

    __slots__ = ('_loop', '_owner', '_deadline_at', '_fired',
                 '_seconds', '_registered')

    def __init__(self, seconds: float) -> None:
        # Left unannotated deliberately: beartype instruments annotated
        # attribute assignments with ``die_if_unbearable``, and it cannot
        # resolve a quoted forward reference from that call site.
        self._loop = None
        self._owner = None
        self._deadline_at: float = _INF
        self._fired = False
        self._seconds = seconds
        self._registered = False

    @property
    def fired(self) -> bool:
        return self._fired

    def _fire_from_scanner(self) -> None:
        """Invoked by [`_tick`][] when ``_deadline_at`` has passed."""
        self._fired = True
        self._deadline_at = _INF
        self._registered = False
        _Scanner._REGISTRY.discard(self)
        if self._owner is not None and not self._owner.done():
            self._owner.cancel()

    def __enter__(self) -> 'WriteDeadline':
        if self._owner is None:
            loop = self._loop
            if loop is None or loop is not _Scanner._LOOP:
                # First arm, or the writer outlived the loop it was built
                # under (worker fork, test isolation).
                loop = self._loop = asyncio.get_running_loop()
            self._fired = False
            self._owner = asyncio.current_task()
            self._deadline_at = loop.time() + self._seconds
            if not self._registered:
                _ensure_scanner_running(loop)
                _Scanner._REGISTRY.add(self)
                self._registered = True
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        task = asyncio.current_task()
        if self._owner is not task:
            # A concurrent drain that rode along on someone else's window.
            return False
        self._owner = None
        self._deadline_at = _INF
        if self._registered:
            _Scanner._REGISTRY.discard(self)
            self._registered = False
        if self._fired and (exc_type is None or exc_type is asyncio.CancelledError):
            if task is not None:
                # Clear the cancel ``_fire_from_scanner`` requested; the
                # drain either raised CancelledError (suppressed here) or
                # completed in the same tick the scanner fired.  Either
                # way the surrounding task must not stay cancelled.
                task.uncancel()
            raise TimeoutError
        return False


class WsIdleWatchdog:
    """Service quiet WebSocket control/deferred reads through the shared scanner.

    Receive/send touch activity; disarm removes the watch. Quiet connections
    invoke the callback roughly each tick without per-connection timer handles.
    """

    __slots__ = ('_loop', '_idle_s', '_deadline_at', '_callback', '_registered')

    def __init__(self, callback, *, idle_s: float | None = None) -> None:
        self._loop = asyncio.get_running_loop()
        self._idle_s: float = _TICK_S if idle_s is None else idle_s
        self._deadline_at: float = _INF
        self._callback = callback
        self._registered = False

    def touch(self) -> None:
        """Mark connection activity; the watchdog goes quiet for _idle_s."""
        self._deadline_at = self._loop.time() + self._idle_s
        if not self._registered:
            _ensure_scanner_running(self._loop)
            _Scanner._REGISTRY.add(self)
            self._registered = True

    def _fire_from_scanner(self) -> None:
        """Invoked by [`_tick`][] when the connection has been idle."""
        # Re-arm first: the callback may schedule work, but this watchdog
        # keeps watching (once per tick) until disarm().
        self._deadline_at = self._loop.time() + self._idle_s
        cb = self._callback
        if cb is not None:
            cb()

    def disarm(self) -> None:
        """Stop watching.  Idempotent."""
        self._deadline_at = _INF
        if self._registered:
            _Scanner._REGISTRY.discard(self)
            self._registered = False
