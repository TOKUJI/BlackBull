"""Per-connection rescheduled deadline (Sprint 26 Phase B — scanner form).

Replaces ``async with asyncio.timeout(...)`` on the per-request hot
path.  Sprint 23 used one ``loop.call_later`` ``TimerHandle`` per
connection, cancelled and rescheduled at every phase transition.
Sprint 26 Phase B replaces the per-arm ``call_later`` with a
per-process tick scanner: one singleton ``TimerHandle`` re-arms itself
every :data:`_TICK_S` and walks the registry of armed
:class:`ConnectionDeadline` instances for expirations.  Per-arm cost
drops from ~1.7 µs to ~0.34 µs (WSL2 microbench, Sprint 26 Phase B
spike).

Trade-off: a fired deadline lands within ``[now, now + _TICK_S]``
rather than at the exact requested instant.  At the default
``_TICK_S = 0.3 s`` this is ~3 % slop on the tightest configurable
deadline (``BB_HEADER_TIMEOUT`` default 10 s) and ~1 % on the
``body_timeout`` default 30 s.  Tune via ``BB_DEADLINE_TICK_MS``
(milliseconds, default 300, floor 10).

The scanner is loop-scoped and lazily started on the first ``arm``;
it quiesces (cancels its own handle) when the registry empties and
auto-resurrects on the next ``arm``.  Each worker process has its
own scanner.
"""
from __future__ import annotations

import asyncio
import os
from typing import ClassVar


_TICK_S: float = max(0.01,
                     float(os.environ.get('BB_DEADLINE_TICK_MS', '300')) / 1000.0)
_INF = float('inf')


class _Scanner:
    """Per-process singleton state for the tick scanner.

    Stored as class attributes rather than a module-global dict so the
    invariant "at most one handle armed at a time" is enforced by the
    type system.
    """

    _LOOP: ClassVar['asyncio.AbstractEventLoop | None'] = None
    _HANDLE: ClassVar['asyncio.TimerHandle | None'] = None
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
    ``TimeoutError`` via :meth:`guard` (the common case) or manually
    by checking :attr:`fired`.

    Why a scanner instead of one ``call_later`` per arm?  At
    saturation the per-arm path costs ~1.7 µs (TimerHandle + heap
    push + cancel).  The scanner replaces that with one
    ``loop.time()`` call + one comparison + one set membership check
    on the registry (~0.34 µs).  Sprint 26 Phase B microbench: 80 %
    per-call reduction; cascade-multiplied estimate ~15–25 % B1.
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

        Safe to call repeatedly.  Resets :attr:`fired` so a recovered
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
        """Invoked by :func:`_tick` when ``_deadline_at`` has passed."""
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
        — a fired deadline manifests as ``TimeoutError``.  The
        same-loop-iteration race where the underlying read completes
        *and* the deadline fires in the same tick is treated as a
        timeout (same convention as ``asyncio.timeout``).

        Returns ``self`` rather than allocating a wrapper object.  Safe
        because each connection owns its own ``ConnectionDeadline``
        and uses it sequentially from a single task.
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
