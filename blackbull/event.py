"""Event dispatch with three lifetime contracts.

Interceptors run in order and propagate failures. Blocking observers finish
before emit returns but isolate failures. Detached observers do not extend
the event lifetime; use blocking observers for cleanup that must finish first.
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
        # Invalidate derived listener caches on every registration.
        self.generation: int = 0
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
        """Wait for observer quiescence, including tasks emitted by observers.

        Return False on timeout, True on quiescence; never cancel pending work.
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
        """Wait up to shutdown_timeout for quiescence; warn and cancel remaining observers.
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
