"""Event-driven dispatcher.

Implements the minimal Pub/Sub dispatcher used by ``BlackBull.on`` /
``BlackBull.intercept``.  Two delivery modes are supported:

- **Interception** (``intercept``): handlers are awaited in registration order;
  exceptions propagate to the emitter and abort subsequent interceptors.
- **Observation** (``on``): handlers are scheduled as independent
  ``asyncio.Task``s (fire-and-forget); exceptions are caught and logged and
  never reach the emitter or other observers.
"""
import asyncio
import logging
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
    """

    def __init__(self) -> None:
        self._observers: dict[str, list[EventHandler]] = {}
        self._interceptors: dict[str, list[EventHandler]] = {}

    def on(self, event_name: str, handler: EventHandler) -> None:
        """Register an observation handler for ``event_name``."""
        self._observers.setdefault(event_name, []).append(handler)

    def intercept(self, event_name: str, handler: EventHandler) -> None:
        """Register an interception handler for ``event_name``."""
        self._interceptors.setdefault(event_name, []).append(handler)

    async def emit(self, event: Event) -> None:
        """Dispatch ``event`` to all registered handlers.

        Interception handlers are awaited in registration order; their
        exceptions propagate.  Observation handlers are scheduled as
        independent tasks and their exceptions are isolated.
        """
        for h in self._interceptors.get(event.name, []):
            await h(event)

        for h in self._observers.get(event.name, []):
            asyncio.create_task(self._safe_observe(h, event))

    async def _safe_observe(self, handler: EventHandler, event: Event) -> None:
        try:
            await handler(event)
        except Exception:
            logger.exception("Observer failed for event %r", event.name)
