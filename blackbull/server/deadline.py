"""Loop-scoped shared deadline scanner.

Enforcement can lag by one BB_DEADLINE_TICK_MS interval. The registry must
quiesce when empty and resume on the next arm; each worker owns its scanner.
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import ClassVar
from ..env import read_setting


logger = logging.getLogger(__name__)

TICK_S: float = max(0.01, read_setting('deadline_tick_ms') / 1000.0)
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
        if dl._at <= now:
            try:
                dl._expire()
            except Exception:
                # One failing callback must not end enforcement for the rest.
                logger.exception('deadline callback failed')
    if _Scanner._REGISTRY:
        _Scanner._HANDLE = loop.call_later(TICK_S, _tick)
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
        _Scanner._HANDLE = loop.call_later(TICK_S, _tick)


class _Armed:
    """``arm`` (re)sets the deadline from now; ``seconds <= 0`` or ``disarm`` drops it."""

    __slots__ = ('_loop', '_at', '_registered')

    def arm(self, seconds: float) -> None:
        if seconds <= 0:
            self.disarm()
            return
        loop = self._loop
        if loop is None:
            loop = self._loop = asyncio.get_running_loop()
        self._at = loop.time() + seconds
        if not self._registered:
            _ensure_scanner_running(loop)
            _Scanner._REGISTRY.add(self)
            self._registered = True

    def when(self) -> float:
        """Loop time it expires at; ``inf`` while disarmed."""
        return self._at

    def disarm(self) -> None:
        self._at = _INF
        if self._registered:
            _Scanner._REGISTRY.discard(self)
            self._registered = False


class Deadline(_Armed):
    """A deadline the shared scanner enforces: *on_expiry* runs once it passes,
    on the loop, synchronously, at most one tick late.
    """

    __slots__ = ('_on_expiry',)

    def __init__(self, on_expiry: Callable[[], None]) -> None:
        self._loop = None
        self._at = _INF
        self._registered = False
        self._on_expiry = on_expiry

    def _expire(self) -> None:
        self.disarm()
        self._on_expiry()


class Guard(_Armed):
    """Bound the body of ``with guard.within(seconds):`` and raise ``TimeoutError``.

    Expiry cancels the task that created the guard; a read that completes in
    the tick the deadline fires is still a timeout, as with ``asyncio.timeout``.
    """

    __slots__ = ('_task', '_seconds', '_fired')

    def __init__(self, seconds: float = 0.0) -> None:
        self._loop = None
        self._at = _INF
        self._registered = False
        self._task = asyncio.current_task()
        self._seconds = seconds
        self._fired = False

    def within(self, seconds: float) -> 'Guard':
        self._seconds = seconds
        return self

    def _expire(self) -> None:
        self.disarm()
        self._fired = True
        task = self._task
        if task is not None and not task.done():
            task.cancel()

    # Enter and exit inline arm and disarm: they run on every request.
    def __enter__(self) -> 'Guard':
        self._fired = False
        if self._seconds > 0:
            loop = self._loop
            if loop is None:
                loop = self._loop = asyncio.get_running_loop()
            self._at = loop.time() + self._seconds
            if not self._registered:
                _ensure_scanner_running(loop)
                _Scanner._REGISTRY.add(self)
                self._registered = True
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        self._at = _INF
        if self._registered:
            _Scanner._REGISTRY.discard(self)
            self._registered = False
        if self._fired and (exc_type is None or exc_type is asyncio.CancelledError):
            # The cancel this guard requested must not outlive it.
            self._task.uncancel()
            raise TimeoutError
        return False


class SharedGuard(_Armed):
    """A ``Guard`` several tasks may enter at once, as concurrent drains do.

    The first to enter owns the deadline and is the one cancelled; a task that
    enters while another owns it rides along without rearming it.
    """

    __slots__ = ('_task', '_seconds', '_fired')

    def __init__(self, seconds: float) -> None:
        # No task yet: a writer may be built outside the loop.
        self._loop = None
        self._at = _INF
        self._registered = False
        self._task = None
        self._seconds = seconds
        self._fired = False

    _expire = Guard._expire

    # Enter and exit inline arm and disarm: a writer drains once per response.
    def __enter__(self) -> 'SharedGuard':
        if self._task is None and self._seconds > 0:
            loop = self._loop
            if loop is None or loop is not _Scanner._LOOP:
                # First enter, or the writer outlived the loop it was armed under.
                loop = self._loop = asyncio.get_running_loop()
                self._registered = False
            self._task = asyncio.current_task()
            self._fired = False
            self._at = loop.time() + self._seconds
            if not self._registered:
                _ensure_scanner_running(loop)
                _Scanner._REGISTRY.add(self)
                self._registered = True
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        task = asyncio.current_task()
        if self._task is not task:
            return False
        self._task = None
        self._at = _INF
        if self._registered:
            _Scanner._REGISTRY.discard(self)
            self._registered = False
        if self._fired and (exc_type is None or exc_type is asyncio.CancelledError):
            task.uncancel()
            raise TimeoutError
        return False
