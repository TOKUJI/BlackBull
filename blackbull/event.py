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
        detail: Arbitrary per-event data.  ``detail`` is used (rather than
            ``payload``) to avoid colliding with HTTP/2 and WebSocket
            protocol terminology already used in the codebase.
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
    :meth:`aclose`.  The drain timeout is configured at construction time
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

    def on(self, event_name: str, handler: EventHandler,
           blocking: bool = False) -> None:
        """Register an observation handler for ``event_name``.

        With ``blocking=False`` (the default) the handler is scheduled as an
        independent task when the event fires — it never delays the emitter.
        With ``blocking=True`` the handler is awaited in registration order
        before ``emit`` returns, so a side effect (e.g. resource cleanup) is
        guaranteed to finish within the event's lifetime; its exceptions are
        still isolated (logged, never propagated).
        """
        if blocking:
            self._blocking_observers[event_name].append(handler)
        else:
            self._observers[event_name].append(handler)
        self.generation += 1

    def intercept(self, event_name: str, handler: EventHandler) -> None:
        """Register an interception handler for ``event_name``."""
        self._interceptors[event_name].append(handler)
        self.generation += 1

    def has_listeners(self, event_name: str) -> bool:
        """Return True if any interceptor or observer is registered for ``event_name``.

        Hot path: callers use this to skip detail-dict / ``Event`` construction
        when no one will receive the event.  ``defaultdict.get`` returns ``None``
        for missing keys without inserting an empty list.
        """
        return (bool(self._interceptors.get(event_name))
                or bool(self._blocking_observers.get(event_name))
                or bool(self._observers.get(event_name)))

    async def emit(self, event: Event) -> None:
        """Dispatch ``event`` to all registered handlers.

        Delivery order:

        1. **Interceptors** — awaited in registration order; their exceptions
           propagate (and abort the remaining interceptors).
        2. **Blocking observers** — awaited in registration order; their
           exceptions are caught and logged (isolated).  ``emit`` does not
           return until they finish, so cleanup registered here is guaranteed
           to complete within the event's lifetime.
        3. **Detached observers** — scheduled as independent tasks (isolated),
           tracked so they can be drained at shutdown via :meth:`aclose`.
        """
        for h in self._interceptors.get(event.name, []):
            await h(event)

        for h in self._blocking_observers.get(event.name, []):
            await self._safe_observe(h, event)

        for h in self._observers.get(event.name, []):
            task = asyncio.create_task(self._safe_observe(h, event))
            self._pending_tasks.add(task)
            task.add_done_callback(self._pending_tasks.discard)

    async def _safe_observe(self, handler: EventHandler, event: Event) -> None:
        try:
            await handler(event)
        except Exception:
            logger.exception("Observer failed for event %r", event.name)

    async def aclose(self) -> None:
        """Drain pending observer tasks during shutdown.

        Waits up to ``shutdown_timeout`` seconds (configured at
        construction) for all in-flight observer tasks to complete.  Any
        tasks still running after the timeout are logged at WARNING and
        cancelled.
        """
        if not self._pending_tasks:
            return

        pending = list(self._pending_tasks)
        _done, still_pending = await asyncio.wait(
            pending, timeout=self._shutdown_timeout,
        )

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
