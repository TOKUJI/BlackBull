"""Event-driven dispatcher.

Implements the minimal Pub/Sub dispatcher used by ``BlackBull.on`` /
``BlackBull.intercept``.  Three delivery modes are supported:

- **Interception** (``intercept``): handlers are awaited in registration order;
  exceptions propagate to the emitter and abort subsequent interceptors.
- **Blocking observation** (``on(..., blocking=True)``): handlers are awaited in
  registration order *before* ``emit`` returns, but their exceptions are caught
  and logged — they never reach the emitter or abort siblings.  This is the
  "observe but block" mode: use it when a side effect must *complete* within the
  event's lifetime (resource cleanup on ``scope_completed``) yet must not be
  able to break the thing that emitted it.
- **Observation** (``on``): handlers are scheduled as independent
  ``asyncio.Task``s (fire-and-forget); exceptions are caught and logged and
  never reach the emitter or other observers.

The two observation modes share isolation (a failing observer is contained);
they differ only in whether ``emit`` waits for them.  Blocking is the right
default for cleanup that must finish before the request context is gone;
detached is right for telemetry that must not add latency to the hot path.
"""
import asyncio
import logging
from collections import defaultdict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Event:
    """An immutable message dispatched through ``EventDispatcher``.

    Attributes:
        name: The event name (e.g. ``"app_startup"``).
        detail: Arbitrary per-event data.
    """

    name: str
    detail: dict = field(default_factory=dict)


EventHandler = Callable[[Event], Awaitable[None]]


class EventDispatcher:
    """Minimal Pub/Sub dispatcher with split interception/observation paths.

    Interception handlers (``intercept``) are awaited in registration order;
    their exceptions propagate to the emitter.  Observation handlers (``on``)
    are scheduled via ``asyncio.create_task`` (fire-and-forget) and their
    exceptions are caught and logged — they never reach the emitter.

    Observer tasks are tracked so they can be drained at shutdown via
    [`aclose`][].  The drain timeout is configured at construction time
    (``shutdown_timeout``); any task still running after the timeout is
    logged at WARNING and cancelled.
    """

    def __init__(self, shutdown_timeout: float = 5.0) -> None:
        self._observers: defaultdict[str, list[EventHandler]] = defaultdict(list)
        self._blocking_observers: defaultdict[str, list[EventHandler]] = defaultdict(list)
        self._interceptors: defaultdict[str, list[EventHandler]] = defaultdict(list)
        self._pending_tasks: set[asyncio.Task] = set()
        self._shutdown_timeout = shutdown_timeout
        # Monotonic counter bumped on every registration.  Hot-path callers
        # (e.g. EventAggregator.has_any_request_listeners) cache a derived
        # boolean keyed on this value, recomputing only when it changes —
        # listeners are almost always registered before serving, so the
        # per-request cost collapses to one int read + compare.
        self.generation: int = 0
        # Names with at least one handler of any kind.  ``has_listeners`` is
        # called several times per request — once per lifecycle emit site — so
        # it answers from this set rather than probing three dicts.  Handlers
        # are only ever added, so the set never needs to shrink.
        self._registered: set[str] = set()

    def on(self, event_name: str, handler: EventHandler,
           blocking: bool = False) -> None:
        """Register an observation handler for ``event_name``.

        With ``blocking=False`` (the default) the handler is scheduled as an
        independent task when the event fires — it never delays the emitter.
        With ``blocking=True`` the handler is awaited in registration order,
        subject to ``emit()``'s timeout and cancellation; its exceptions are
        logged.
        """
        if blocking:
            self._blocking_observers[event_name].append(handler)
        else:
            self._observers[event_name].append(handler)
        self._registered.add(event_name)
        self.generation += 1

    def intercept(self, event_name: str, handler: EventHandler) -> None:
        """Register an interception handler for ``event_name``."""
        self._interceptors[event_name].append(handler)
        self._registered.add(event_name)
        self.generation += 1

    def has_listeners(self, event_name: str) -> bool:
        """Return True if any interceptor or observer is registered for ``event_name``.

        One set lookup, and it never inserts an entry for a name nobody has
        registered, so a caller may ask on every event.
        """
        return event_name in self._registered

    async def emit(self, event: Event, *, timeout: float | None = None) -> None:
        """Dispatch ``event`` to all registered handlers.

        Delivery order:

        1. **Interceptors** — awaited in registration order; their exceptions
           propagate (and abort the remaining interceptors).
        2. **Blocking observers** — awaited in registration order; their
           exceptions are caught and logged.
        3. **Detached observers** — scheduled as independent tasks (isolated),
           tracked so they can be drained at shutdown via [`aclose`][].

        ``timeout`` limits the wait for each interceptor and blocking
        observer.  On expiry, cancellation is requested, a warning
        identifies the handler, and dispatch continues without waiting for
        it to stop.  ``None`` disables this limit.
        """
        for h in self._interceptors.get(event.name, []):
            if timeout is None:
                await h(event)
            else:
                await self._bounded_run(h, event, timeout)

        for h in self._blocking_observers.get(event.name, []):
            if timeout is None:
                await self._safe_observe(h, event)
            else:
                await self._bounded_run(
                    lambda e, _h=h: self._safe_observe(_h, e),
                    event, timeout, name=getattr(h, '__qualname__', repr(h)))

        for h in self._observers.get(event.name, []):
            task = asyncio.create_task(self._safe_observe(h, event))
            self._pending_tasks.add(task)
            task.add_done_callback(self._pending_tasks.discard)

    async def _bounded_run(self, handler, event: Event, timeout: float,
                           *, name: str | None = None) -> bool:
        """Return False on timeout; otherwise return True or propagate the error."""
        who = name or getattr(handler, '__qualname__', repr(handler))
        task = asyncio.ensure_future(handler(event))
        # wait_for would also wait for cancellation to finish.
        try:
            done, _pending = await asyncio.wait({task}, timeout=timeout)
        except asyncio.CancelledError:
            task.cancel()
            raise
        if not done:
            task.cancel()
            logger.warning(
                'Event %r: handler %s cancelled after its %.1fs dispatch budget',
                event.name, who, timeout)
            return False
        task.result()
        return True

    async def _safe_observe(self, handler: EventHandler, event: Event) -> None:
        try:
            await handler(event)
        except Exception:
            logger.exception("Observer failed for event %r", event.name)

    async def drain(self, timeout: float = 5.0) -> bool:
        """Wait until no detached observer task is outstanding.

        Returns ``True`` on quiescence, ``False`` if *timeout* ran out
        first.  **Nothing is cancelled either way** — that is the whole
        difference from [`aclose`][], which is a shutdown operation and
        kills what overruns.  A test helper that cancelled the work it was
        asked to observe would make the side-effect it exists to reveal
        unobservable.

        Drains to *quiescence*, not to a snapshot: an observer may itself
        emit, so the set is re-read after every wait and a second generation
        is waited for too.
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            pending = [t for t in self._pending_tasks if not t.done()]
            if not pending:
                return True
            remaining = deadline - loop.time()
            if remaining <= 0:
                return False
            await asyncio.wait(pending, timeout=remaining)

    async def aclose(self) -> None:
        """Drain pending observer tasks during shutdown.

        Waits up to ``shutdown_timeout`` seconds (configured at
        construction) for all in-flight observer tasks to complete.  Any
        tasks still running after the timeout are logged at WARNING and
        cancelled.

        Drains to quiescence through [`drain`][], so an observer that emits
        is waited for too.

        The cost is that a pathological observer chain can hold shutdown for
        the full budget rather than returning early.  Returning early is the
        wrong answer, not a cheaper one, and ``shutdown_timeout`` is the
        ceiling — so an observer chain that never quiesces is a bounded
        latency cost at shutdown.
        """
        if not self._pending_tasks:
            return

        if await self.drain(self._shutdown_timeout):
            return

        still_pending = [t for t in self._pending_tasks if not t.done()]
        if still_pending:
            for task in still_pending:
                coro = task.get_coro()
                name = getattr(coro, '__qualname__', repr(coro))
                logger.warning(
                    "Observer task did not finish within %.1fs and will be "
                    "cancelled: %s",
                    self._shutdown_timeout, name,
                )
                task.cancel()

            await asyncio.gather(*still_pending, return_exceptions=True)
