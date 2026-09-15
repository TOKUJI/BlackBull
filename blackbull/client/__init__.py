"""BlackBull's own async client, speaking the server's wire code from the
other side.

Pure Python, no third-party protocol library, and the same frame and parser
implementations the server uses — which is what makes it useful for testing
BlackBull against its own bytes and for pointing at a deliberately broken
peer.

Pick a client by naming the protocol, or let ALPN decide:

- [`Client`][blackbull.client.Client] negotiates over TLS and hands back
  whichever of the two it chose.  With no TLS there is no ALPN to read, so it
  is always HTTP/1.1 — h2c is supported, but only by naming
  [`HTTP2Client`][blackbull.client.HTTP2Client] yourself.
- [`WebSocketClient`][blackbull.client.WebSocketClient] (an HTTP/1.1 upgrade)
  and [`WebSocketH2Client`][blackbull.client.WebSocketH2Client] (extended
  CONNECT) are chosen explicitly, never by negotiation.

Every client is an async context manager, and the connection lives exactly as
long as its ``async with`` block.  Everything they raise derives from
[`ClientError`][blackbull.client.ClientError], so one ``except`` catches the
family and the subclasses tell the causes apart.

The scenario primitives re-exported here — ``Scenario``, ``Step``, ``Abort``
and the rest — belong to ``blackbull.fault_injection``; this is an alias, not
a second implementation.

``docs/guide/client.md`` answers what the defaults do and do not bound.
"""
from .client import Client
from .http1 import (HTTP1Client, HTTP1RequestSender, HTTP1ResponseRecipient,
                    HTTP1UpgradeSession)
from .http2 import ClientResponse, HTTP2Client
from .response import ResponderFactory
from blackbull.fault_injection.scenario_h1 import (
    Abort,
    ReadResponse,
    Scenario,
    ScenarioResult,
    SendRawBytes,
    Sleep,
    Step,
)
from .websocket import WebSocketClient, WebSocketSession
from .websocket_h2 import WebSocketH2Client, WebSocketH2Session
from .exceptions import (
    ClientError,
    ConnectionError,
    HandshakeError,
    ProtocolError, ResponseTooLarge,
    StreamReset,
)

__all__ = [
    'Abort',
    'Client',
    'ClientResponse',
    'HTTP1Client',
    'HTTP1RequestSender',
    'HTTP1ResponseRecipient',
    'HTTP1UpgradeSession',
    'HTTP2Client',
    'ReadResponse',
    'ResponderFactory',
    'Scenario',
    'ScenarioResult',
    'SendRawBytes',
    'Sleep',
    'Step',
    'WebSocketClient',
    'WebSocketH2Client',
    'WebSocketH2Session',
    'WebSocketSession',
    'ClientError',
    'ConnectionError',
    'HandshakeError',
    'ProtocolError',
    'ResponseTooLarge',
    'StreamReset',
]


# Unannotated on purpose: beartype leaves an unannotated function unwrapped,
# and a wrapper frame would take stacklevel=2 away from the caller's line
# (tests/unit/test_deprecated_send_bytes_spellings.py).
def __getattr__(name):
    """PEP 562 — ``SendBytes`` is resolved only when a caller names it, so the
    deprecation warning reaches that caller and ``import *`` stays silent."""
    if name == 'SendBytes':
        import warnings  # noqa: PLC0415
        warnings.warn(
            f"{__name__}.SendBytes is deprecated; use SendRawBytes.  Removal no "
            "earlier than 2027-08-19.",
            DeprecationWarning, stacklevel=2)
        return SendRawBytes
    raise AttributeError(f'module {__name__!r} has no attribute {name!r}')
