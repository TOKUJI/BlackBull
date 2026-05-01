import asyncio
from typing import Any

from blackbull.event import EventDispatcher


class EventAggregator:
    """Translates Level A Actor messages into Level B EventDispatcher calls.

    This class is *not* an Actor — it has no inbox and no lifecycle of its own.
    It is instantiated once per application and passed by reference to each
    Actor at construction time.

    Each method corresponds to one Level B event defined in ActorDesign.md.
    Methods are called by Actors; they must always be called from the event
    loop thread.

    Note:
        Do not export this class from ``blackbull/__init__.py``.
        It is a framework-internal component.
    """

    def __init__(self, dispatcher: EventDispatcher) -> None:
        self._dispatcher = dispatcher

    # ------------------------------------------------------------------
    # Server lifecycle
    # ------------------------------------------------------------------

    async def on_app_startup(self) -> None:
        """Fire Level B ``app_startup``."""
        await self._dispatcher.emit("app_startup", {})

    async def on_app_shutdown(self) -> None:
        """Fire Level B ``app_shutdown``."""
        await self._dispatcher.emit("app_shutdown", {})

    # ------------------------------------------------------------------
    # Request lifecycle
    # ------------------------------------------------------------------

    async def on_request_received(self, scope: dict[str, Any]) -> None:
        """Fire Level B ``request_received``."""
        await self._dispatcher.emit("request_received", {"scope": scope})

    async def on_before_handler(
        self, scope: dict[str, Any], receive: Any, send: Any, call_next: Any
    ) -> None:
        """Fire Level B ``before_handler`` (intercept)."""
        await self._dispatcher.emit_intercept(
            "before_handler", scope, receive, send, call_next
        )

    async def on_after_handler(
        self, scope: dict[str, Any], exception: BaseException | None = None
    ) -> None:
        """Fire Level B ``after_handler``."""
        await self._dispatcher.emit(
            "after_handler", {"scope": scope, "exception": exception}
        )

    async def on_request_completed(self, scope: dict[str, Any]) -> None:
        """Fire Level B ``request_completed``."""
        await self._dispatcher.emit("request_completed", {"scope": scope})

    async def on_request_disconnected(self, scope: dict[str, Any]) -> None:
        """Fire Level B ``request_disconnected``."""
        await self._dispatcher.emit("request_disconnected", {"scope": scope})

    async def on_error(
        self, scope: dict[str, Any], exception: BaseException
    ) -> None:
        """Fire Level B ``error``."""
        await self._dispatcher.emit(
            "error", {"scope": scope, "exception": exception}
        )

    # ------------------------------------------------------------------
    # WebSocket lifecycle
    # ------------------------------------------------------------------

    async def on_websocket_connected(self, scope: dict[str, Any]) -> None:
        """Fire Level B ``websocket_connected``."""
        await self._dispatcher.emit("websocket_connected", {"scope": scope})

    async def on_websocket_message(
        self, scope: dict[str, Any], message: dict[str, Any]
    ) -> None:
        """Fire Level B ``websocket_message``."""
        await self._dispatcher.emit(
            "websocket_message", {"scope": scope, "message": message}
        )

    async def on_websocket_disconnected(self, scope: dict[str, Any]) -> None:
        """Fire Level B ``websocket_disconnected``."""
        await self._dispatcher.emit("websocket_disconnected", {"scope": scope})
