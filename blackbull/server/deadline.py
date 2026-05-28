"""Per-connection rescheduled deadline (Sprint 23).

Replaces ``async with asyncio.timeout(...)`` on the per-request hot
path.  Each ``asyncio.timeout`` enter/exit pair allocates a ``Timeout``
object, schedules a ``call_later`` handle, and registers a cancellation
marker on the current task — collectively ~10 % of per-worker CPU at
saturation (Sprint 21 Phase B py-spy finding).

:class:`ConnectionDeadline` keeps a single ``call_later`` ``TimerHandle``
per connection that gets cancelled and rescheduled at every phase
transition (sniff → headers → body chunk → keep-alive idle).  Per-phase
semantics — most importantly the per-chunk body deadline that mirrors
nginx ``client_body_timeout`` — are preserved by calling :meth:`arm`
once per chunk.
"""
from __future__ import annotations

import asyncio
from contextlib import contextmanager


class ConnectionDeadline:
    """One reusable timer per connection.

    The instance binds to the task that constructed it (in practice, the
    connection actor's task).  When the timer fires, that task is
    cancelled — the cancellation propagates into whichever
    ``reader.readuntil`` / ``read`` / ``readexactly`` is currently
    awaiting.  Call sites translate the cancellation into ``TimeoutError``
    via :meth:`guard` (the common case) or manually by checking
    :attr:`fired`.

    Why not just keep ``asyncio.timeout``?  Three reasons surface in the
    profile: the ``Timeout`` object allocation, the per-call
    cancel-scope registration, and the ``TaskGroup``-style task-state
    bookkeeping in ``__aenter__``/``__aexit__``.  A bare
    ``TimerHandle`` skips all three.
    """

    __slots__ = ('_loop', '_task', '_handle', '_fired')

    def __init__(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._task = asyncio.current_task()
        self._handle: asyncio.TimerHandle | None = None
        self._fired = False

    def arm(self, seconds: float) -> None:
        """(Re-)set the deadline; ``seconds <= 0`` disables it.

        Safe to call repeatedly; cancels any pending handle first.
        Resets :attr:`fired` so a recovered deadline can be reused
        across phases on the same connection.
        """
        if self._handle is not None:
            self._handle.cancel()
            self._handle = None
        self._fired = False
        if seconds > 0:
            self._handle = self._loop.call_later(seconds, self._fire)

    def disarm(self) -> None:
        """Cancel the pending timer, if any.  Idempotent."""
        if self._handle is not None:
            self._handle.cancel()
            self._handle = None

    def _fire(self) -> None:
        self._fired = True
        self._handle = None
        if self._task is not None and not self._task.done():
            self._task.cancel()

    @property
    def fired(self) -> bool:
        return self._fired

    @contextmanager
    def guard(self, seconds: float):
        """Arm the deadline, yield, then disarm — translating
        deadline-driven ``CancelledError`` into :class:`TimeoutError`
        and clearing the task's cancel state so subsequent awaits are
        not spuriously cancelled.

        Caller pattern::

            with dl.guard(cfg.header_timeout):
                await reader.readuntil(...)

        Matches the observable behaviour of ``async with asyncio.timeout(d):``
        — a fired deadline manifests as ``TimeoutError``.  The
        same-loop-iteration race where the underlying read completes
        *and* the deadline fires in the same tick is treated as a
        timeout (same convention as ``asyncio.timeout``).
        """
        self.arm(seconds)
        try:
            yield
        except asyncio.CancelledError:
            if not self._fired:
                raise
            # Fall through to the disarm + TimeoutError raise below.
        finally:
            self.disarm()
        if self._fired:
            task = asyncio.current_task()
            if task is not None:
                # Clear the cancel that ``_fire`` requested; the read
                # either raised already (handled above) or completed
                # normally with a still-pending cancel.  Either way
                # the surrounding task must not stay in cancelled
                # state.
                task.uncancel()
            raise TimeoutError
