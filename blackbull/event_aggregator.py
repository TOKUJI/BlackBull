"""Boundary from internal actor traffic to application events.

Keep application event names and detail shapes transport-independent.
See docs/guide/events.md for the public contract.
"""
from blackbull.asgi import WebSocketReceiveEvent
from blackbull.event import Event, EventDispatcher


def request_received_event(conn) -> Event:
    client = conn.client or ('-',)
    return Event('request_received', detail={
        'conn':        conn,
        'client_ip':    str(client[0]),
        'method':       conn.method,
        'path':         conn.path,
        'http_version': conn.http_version,
        'headers':      conn.headers,
    })


def handler_event(name: str, conn, handler, exception=None) -> Event:
    """``before_handler``, or ``after_handler`` with *exception*."""
    detail = {
        'conn': conn,
        'client_ip': conn.client[0] if conn.client else '',
        'method': conn.method,
        'path': conn.path,
        'handler': getattr(handler, '__name__', type(handler).__name__),
    }
    if name == 'after_handler':
        detail['exception'] = exception
    return Event(name, detail=detail)


def request_completed_event(conn) -> Event:
    log = conn.state.get('access_log')
    client = conn.client or ('-',)
    return Event('request_completed', detail={
        'conn':          conn,
        'client_ip':      str(client[0]),
        'method':         conn.method,
        'path':           conn.path,
        'http_version':   conn.http_version,
        'status':         log.status if log else '-',
        'response_bytes': log.response_bytes if log else 0,
        'duration_ms':    log.duration_ms() if log else 0.0,
    })


def scope_completed_event(conn, exception) -> Event:
    return Event('scope_completed', {
        'conn':     conn,
        'type':      conn.type,
        'client_ip': str((conn.client or ['-'])[0]),
        'path':      conn.path,
        'exception': exception,
    })


def _ws_fields(conn):
    """Read ``(client, connection_id, path)`` for a WebSocket event detail.

    *conn* is the native [`Connection`][blackbull.connection.Connection] the
    WebSocket actor holds — the convenience keys on a WS event detail are
    lifted off the same object the listener gets as ``detail['conn']``.
    """
    return conn.client, conn.connection_id, conn.path


class EventAggregator:
    """Translates Level A Actor messages into Level B EventDispatcher calls.

    Not an Actor: one per application, passed to each Actor at construction
    and called from the event-loop thread.  Each ``on_*`` method is one Level B
    event of the Events guide.  Framework-internal: do not export it from
    ``blackbull/__init__.py``.
    """

    def __init__(self, dispatcher: EventDispatcher) -> None:
        self._dispatcher = dispatcher

    async def _emit_if_listened(self, name: str, detail: dict) -> None:
        if self._dispatcher.has_listeners(name):
            await self._dispatcher.emit(Event(name, detail))

    async def on_error(self, conn, exception: BaseException) -> None:
        """Fire ``error``; *conn* is ``{}`` for a failure outside any request."""
        await self._emit_if_listened('error', {'conn': conn, 'exception': exception})

    async def on_connection_accepted(self, peername, protocol: str = 'http') -> None:
        """Fire ``connection_accepted``; *protocol* is 'http' on the shared HTTP
        listener (h1/h2 is not known at accept), else the registered name."""
        await self._emit_if_listened('connection_accepted',
                                     {'peername': peername, 'protocol': protocol})

    async def on_connection_closed(self, peername, protocol: str, duration_ms: float) -> None:
        """Fire ``connection_closed``."""
        await self._emit_if_listened('connection_closed', {
            'peername': peername, 'protocol': protocol, 'duration_ms': duration_ms})

    async def on_message_received(self, protocol: str, message_type: str,
                                  payload_size: int) -> None:
        """Fire ``message_received`` (application-level message in)."""
        await self._emit_if_listened('message_received', {
            'protocol': protocol, 'message_type': message_type, 'payload_size': payload_size})

    async def on_message_sent(self, protocol: str, message_type: str,
                              payload_size: int) -> None:
        """Fire ``message_sent`` (application-level message out)."""
        await self._emit_if_listened('message_sent', {
            'protocol': protocol, 'message_type': message_type, 'payload_size': payload_size})

    def has_request_completed_listeners(self) -> bool:
        return self._dispatcher.has_listeners('request_completed')

    def has_request_disconnected_listeners(self) -> bool:
        return self._dispatcher.has_listeners('request_disconnected')

    def has_websocket_message_listeners(self) -> bool:
        return self._dispatcher.has_listeners('websocket_message')

    async def on_request_disconnected(self, conn) -> None:
        """Fire Level B ``request_disconnected``."""
        if not self._dispatcher.has_listeners('request_disconnected'):
            return
        client = conn.client or ['-']
        await self._dispatcher.emit(Event("request_disconnected", {
            'conn':        conn,
            'client_ip':    str(client[0]),
            'method':       conn.method,
            'path':         conn.path,
            'http_version': conn.http_version,
        }))

    async def on_websocket_connected(
        self, conn, subprotocol: str | None = None
    ) -> None:
        """Fire Level B ``websocket_connected`` (``conn`` is a Connection)."""
        client, connection_id, path = _ws_fields(conn)
        await self._dispatcher.emit(Event("websocket_connected", {
            'conn':         conn,
            "connection_id": connection_id,
            "client_ip":     client[0] if client else '',
            "path":          path,
            "subprotocol":   subprotocol,
        }))

    async def on_websocket_message(
        self, conn, message: WebSocketReceiveEvent
    ) -> None:
        """Emit websocket_message with conn/text/bytes detail when listeners exist.
        """
        if not self._dispatcher.has_listeners('websocket_message'):
            return
        await self._dispatcher.emit(
            Event("websocket_message", {
                'conn':  conn,
                'text':  message.get('text'),
                'bytes': message.get('bytes'),
            })
        )

    async def on_websocket_disconnected(
        self, conn, code: int = 1006, *, timeout: float | None = None
    ) -> None:
        """Emit ``websocket_disconnected``; timeout follows ``EventDispatcher.emit``."""
        client, connection_id, path = _ws_fields(conn)
        await self._dispatcher.emit(Event("websocket_disconnected", {
            'conn':         conn,
            "connection_id": connection_id,
            "client_ip":     client[0] if client else '',
            "path":          path,
            "code":          code,
        }), timeout=timeout)
