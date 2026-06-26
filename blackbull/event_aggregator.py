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
        # Cache for has_any_request_listeners(), keyed on the dispatcher's
        # registration generation.  ``-1`` can never equal a real generation
        # (which starts at 0), so the first call always computes.
        self._listeners_cache_gen: int = -1
        self._listeners_cache_val: bool = False

    # ------------------------------------------------------------------
    # Hot-path optimisation: skip the indirection when nothing is listening
    # ------------------------------------------------------------------
    # Every event a request may emit between RequestActor.run() entry and
    # exit.  If ANY of these has a listener, the fast path below must be
    # skipped so the listener still fires.  ``error`` is included because
    # ``on_error`` emits ``Event("error", …)`` — omitting it would silently
    # bypass an ``@app.on('error')``-only handler on the fast path.
    _REQUEST_EVENTS = (
        'request_received', 'before_handler', 'after_handler',
        'request_completed', 'request_disconnected', 'error',
    )

    def has_any_request_listeners(self) -> bool:
        """Return True if *any* request-lifecycle event has listeners.

        Callers on the hot path use this to short-circuit the entire
        ``RequestActor`` indirection when no Level B event handlers are
        registered — matching the pre-Sprint-53 direct ``self._app()``
        call path.

        Result is cached against the dispatcher's registration generation, so
        the 6-event scan runs only when listeners change (effectively once,
        at startup) rather than on every request.
        """
        gen = self._dispatcher.generation
        if gen != self._listeners_cache_gen:
            has = self._dispatcher.has_listeners
            self._listeners_cache_val = any(has(n) for n in self._REQUEST_EVENTS)
            self._listeners_cache_gen = gen
        return self._listeners_cache_val

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
        if not self._dispatcher.has_listeners('request_received'):
            return
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
        if self._dispatcher.has_listeners('before_handler'):
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
        if not self._dispatcher.has_listeners('after_handler'):
            return
        await self._dispatcher.emit(
            Event("after_handler", {"scope": scope, "exception": exception})
        )

    async def on_request_completed(self, scope: dict[str, Any]) -> None:
        """Fire Level B ``request_completed``."""
        if not self._dispatcher.has_listeners('request_completed'):
            return
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
