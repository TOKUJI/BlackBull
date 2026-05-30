"""Server-protocol constants.

``WSCloseCode`` is an ``IntEnum`` for the WebSocket close codes used on the
wire (RFC 6455 §7.4).  ASGI event-type strings used to live here too; they
moved to ``blackbull.asgi.ASGIEvent`` so importing the framework doesn't
drag in the server stack.
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
