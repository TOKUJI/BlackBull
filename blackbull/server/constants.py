"""Server-protocol constants.

``WSCloseCode`` is an ``IntEnum`` for the WebSocket close codes used on the
wire (RFC 6455 §7.4).  ASGI event-type strings belong to
``blackbull.asgi.ASGIEvent``, not here: they are part of the protocol
vocabulary the framework and any external ASGI host share, while this module
is the server's own.
"""
from enum import IntEnum


class WSCloseCode(IntEnum):
    """Close codes for the WebSocket CLOSE frame (RFC 6455 §7.4.1).

    ``NO_STATUS_RCVD``, ``ABNORMAL`` and ``TLS_HANDSHAKE`` name conditions an
    endpoint reports to its own application; those three and the reserved 1004
    are never put on the wire, and a peer that sends one is answered with
    ``PROTOCOL_ERROR``.  An application wanting a code of its own takes the
    private range 4000–4999 (§7.4.2), which has no member here.
    """
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
