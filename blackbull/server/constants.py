"""Protocol constants shared across the server and middleware layers.

``ASGIEvent`` groups ASGI event type strings as class attributes so they can be
used in both equality comparisons and as dotted-name value patterns in
``match``/``case`` statements.

``WSCloseCode`` is an ``IntEnum`` for the WebSocket close codes used on the
wire (RFC 6455 §7.4).
"""
from enum import IntEnum


class WSCloseCode(IntEnum):
    NORMAL         = 1000
    PROTOCOL_ERROR = 1002
    ABNORMAL       = 1006


class ASGIEvent:
    """Namespace for ASGI protocol event type strings (ASGI 3.0 spec)."""

    # HTTP
    HTTP_REQUEST           = 'http.request'
    HTTP_DISCONNECT        = 'http.disconnect'
    HTTP_RESPONSE_START    = 'http.response.start'
    HTTP_RESPONSE_BODY     = 'http.response.body'
    HTTP_RESPONSE_TRAILERS = 'http.response.trailers'
    HTTP_RESPONSE_PUSH     = 'http.response.push'

    # WebSocket
    WS_CONNECT    = 'websocket.connect'
    WS_ACCEPT     = 'websocket.accept'
    WS_RECEIVE    = 'websocket.receive'
    WS_SEND       = 'websocket.send'
    WS_CLOSE      = 'websocket.close'
    WS_DISCONNECT = 'websocket.disconnect'

    # Lifespan
    LIFESPAN_STARTUP           = 'lifespan.startup'
    LIFESPAN_STARTUP_COMPLETE  = 'lifespan.startup.complete'
    LIFESPAN_STARTUP_FAILED    = 'lifespan.startup.failed'
    LIFESPAN_SHUTDOWN          = 'lifespan.shutdown'
    LIFESPAN_SHUTDOWN_COMPLETE = 'lifespan.shutdown.complete'
