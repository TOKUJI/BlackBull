"""Protocol constants shared across the server and middleware layers.

``ASGIEvent`` groups ASGI event type strings as class attributes so they can be
used in both equality comparisons and as dotted-name value patterns in
``match``/``case`` statements.

``WSCloseCode`` is an ``IntEnum`` for the WebSocket close codes used on the
wire (RFC 6455 §7.4).
"""
from enum import IntEnum


class WSCloseCode(IntEnum):
    NORMAL              = 1000
    GOING_AWAY          = 1001
    PROTOCOL_ERROR      = 1002
    UNSUPPORTED_DATA    = 1003
    # 1004 — reserved
    NO_STATUS_RCVD      = 1005   # not allowed on the wire
    ABNORMAL            = 1006   # not allowed on the wire
    INVALID_UTF8        = 1007
    POLICY_VIOLATION    = 1008
    MESSAGE_TOO_BIG     = 1009
    MANDATORY_EXTENSION = 1010
    INTERNAL_ERROR      = 1011
    SERVICE_RESTART     = 1012
    TRY_AGAIN_LATER     = 1013
    BAD_GATEWAY         = 1014
    TLS_HANDSHAKE       = 1015   # not allowed on the wire


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
