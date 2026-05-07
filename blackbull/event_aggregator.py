import asyncio
from typing import Any

from blackbull.event import Event, EventDispatcher


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
        await self._dispatcher.emit(Event("app_startup", {}))

    async def on_app_shutdown(self) -> None:
        """Fire Level B ``app_shutdown``."""
        await self._dispatcher.emit(Event("app_shutdown", {}))

    # ------------------------------------------------------------------
    # Request lifecycle
    # ------------------------------------------------------------------

    async def on_request_received(self, scope: dict[str, Any]) -> None:
        """Fire Level B ``request_received``."""
        client = scope.get('client') or ['-']
        await self._dispatcher.emit(Event("request_received", {
            'scope':        scope,
            'client_ip':    str(client[0]),
            'method':       scope.get('method', '-'),
            'path':         scope.get('path', '-'),
            'http_version': scope.get('http_version', '-'),
            'headers':      scope.get('headers', []),
        }))

    async def on_before_handler(
        self, scope: dict[str, Any], receive: Any, send: Any, call_next: Any
    ) -> None:
        """Fire Level B ``before_handler`` then invoke the next handler."""
        await self._dispatcher.emit(Event("before_handler", {
            'scope':  scope,
            'method': scope.get('method', '-'),
            'path':   scope.get('path', '-'),
        }))
        await call_next(scope, receive, send)

    async def on_after_handler(
        self, scope: dict[str, Any], exception: BaseException | None = None
    ) -> None:
        """Fire Level B ``after_handler``."""
        await self._dispatcher.emit(
            Event("after_handler", {"scope": scope, "exception": exception})
        )

    async def on_request_completed(self, scope: dict[str, Any]) -> None:
        """Fire Level B ``request_completed``."""
        log = scope.get('state', {}).get('access_log')
        client = scope.get('client') or ['-']
        await self._dispatcher.emit(Event("request_completed", {
            'scope':          scope,
            'client_ip':      str(client[0]),
            'method':         scope.get('method', '-'),
            'path':           scope.get('path', '-'),
            'http_version':   scope.get('http_version', '-'),
            'status':         log.status if log else '-',
            'response_bytes': log.response_bytes if log else 0,
            'duration_ms':    log.duration_ms() if log else 0.0,
        }))

    async def on_request_disconnected(self, scope: dict[str, Any]) -> None:
        """Fire Level B ``request_disconnected``."""
        client = scope.get('client') or ['-']
        await self._dispatcher.emit(Event("request_disconnected", {
            'scope':        scope,
            'client_ip':    str(client[0]),
            'method':       scope.get('method', '-'),
            'path':         scope.get('path', '-'),
            'http_version': scope.get('http_version', '-'),
        }))

    async def on_error(
        self, scope: dict[str, Any], exception: BaseException
    ) -> None:
        """Fire Level B ``error``."""
        await self._dispatcher.emit(
            Event("error", {"scope": scope, "exception": exception})
        )

    # ------------------------------------------------------------------
    # WebSocket lifecycle
    # ------------------------------------------------------------------

    async def on_websocket_connected(
        self, scope: dict[str, Any], subprotocol: str | None = None
    ) -> None:
        """Fire Level B ``websocket_connected``."""
        client = scope.get('client')
        await self._dispatcher.emit(Event("websocket_connected", {
            "scope":         scope,
            "connection_id": scope.get('_connection_id', ''),
            "client_ip":     client[0] if client else '',
            "path":          scope.get('path', ''),
            "subprotocol":   subprotocol,
        }))

    async def on_websocket_message(
        self, scope: dict[str, Any], message: dict[str, Any]
    ) -> None:
        """Fire Level B ``websocket_message``."""
        await self._dispatcher.emit(
            Event("websocket_message", {"scope": scope, "message": message})
        )

    async def on_websocket_disconnected(
        self, scope: dict[str, Any], code: int = 1006
    ) -> None:
        """Fire Level B ``websocket_disconnected``."""
        client = scope.get('client')
        await self._dispatcher.emit(Event("websocket_disconnected", {
            "scope":         scope,
            "connection_id": scope.get('_connection_id', ''),
            "client_ip":     client[0] if client else '',
            "path":          scope.get('path', ''),
            "code":          code,
        }))

    # ------------------------------------------------------------------
    # Connection lifecycle
    # ------------------------------------------------------------------

    async def on_connection_accepted(self, peername) -> None:
        """Level A notification — no Level B event fires yet."""
