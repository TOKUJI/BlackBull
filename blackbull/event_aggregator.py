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
        # Generation-keyed cache for the WebSocket receive hot path
        # (has_websocket_message_listeners): a single-event lookup, cached so
        # a 456K-msg/s echo workload never re-scans per frame.
        self._ws_msg_cache_gen: int = -1
        self._ws_msg_cache_val: bool = False

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
    # The request-lifecycle events (request_received / before_handler /
    # after_handler / request_completed) are emitted by BlackBull._dispatch —
    # the single cross-transport emission point (Sprint 64) — so they fire
    # under external ASGI hosts (uvicorn, TestClient) too, exactly once per
    # request.  Only wire-level events remain here.

    async def on_request_disconnected(self, scope: dict[str, Any]) -> None:
        """Fire Level B ``request_disconnected``."""
        if not self._dispatcher.has_listeners('request_disconnected'):
            return
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
        if not self._dispatcher.has_listeners('error'):
            return
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

    def has_websocket_message_listeners(self) -> bool:
        """Return True if any ``websocket_message`` handler is registered.

        The WebSocket receive path calls this per frame to skip the
        ``Event`` + detail-dict allocation and the ``emit`` indirection when
        nothing is listening (the common case on a throughput workload).
        Cached against the dispatcher's registration generation, so the
        lookup runs only when listeners change.
        """
        gen = self._dispatcher.generation
        if gen != self._ws_msg_cache_gen:
            self._ws_msg_cache_val = self._dispatcher.has_listeners('websocket_message')
            self._ws_msg_cache_gen = gen
        return self._ws_msg_cache_val

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

    async def on_connection_accepted(self, peername, protocol: str = 'http') -> None:
        """Fire Level B ``connection_accepted``.

        *protocol* is ``'http'`` for the shared HTTP listener (the h1/h2 split is
        not yet known at accept time) and the registered protocol name (e.g.
        ``'echo'``) for a port-bound non-ASGI connection.
        """
        if not self._dispatcher.has_listeners('connection_accepted'):
            return
        await self._dispatcher.emit(Event('connection_accepted', {
            'peername': peername,
            'protocol': protocol,
        }))

    async def on_connection_closed(
        self, peername, protocol: str, duration_ms: float
    ) -> None:
        """Fire Level B ``connection_closed``."""
        if not self._dispatcher.has_listeners('connection_closed'):
            return
        await self._dispatcher.emit(Event('connection_closed', {
            'peername': peername,
            'protocol': protocol,
            'duration_ms': duration_ms,
        }))

    async def on_message_received(
        self, protocol: str, message_type: str, payload_size: int
    ) -> None:
        """Fire Level B ``message_received`` (application-level message in)."""
        if not self._dispatcher.has_listeners('message_received'):
            return
        await self._dispatcher.emit(Event('message_received', {
            'protocol': protocol,
            'message_type': message_type,
            'payload_size': payload_size,
        }))

    async def on_message_sent(
        self, protocol: str, message_type: str, payload_size: int
    ) -> None:
        """Fire Level B ``message_sent`` (application-level message out)."""
        if not self._dispatcher.has_listeners('message_sent'):
            return
        await self._dispatcher.emit(Event('message_sent', {
            'protocol': protocol,
            'message_type': message_type,
            'payload_size': payload_size,
        }))
