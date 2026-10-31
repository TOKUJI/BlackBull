"""Async protocol clients.

Client selects HTTP/1.1 without TLS; name HTTP2Client explicitly for h2c.
Clients own one connection for their async context. TimeoutError and raw
WebSocket read failures do not all derive from ClientError; see
docs/guide/client.md. Scenario exports alias blackbull.fault_injection.
"""
from ._connect import ClientConnection
from .client import Client
from .http1 import (Fate, HTTP1Client, HTTP1RequestSender, HTTP1ResponseRecipient,
                    HTTP1UpgradeSession)
from .http2 import ClientResponse, HTTP2Client
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
    'ClientConnection',
    'ClientResponse',
    'Fate',
    'HTTP1Client',
    'HTTP1RequestSender',
    'HTTP1ResponseRecipient',
    'HTTP1UpgradeSession',
    'HTTP2Client',
    'ReadResponse',
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


# Unannotated: see tests/unit/test_deprecated_send_bytes_spellings.py::test_the_warning_is_attributed_to_the_callers_line.
def __getattr__(name):
    """Deprecated SendBytes alias for SendRawBytes (BBD005).
    """
    if name == 'SendBytes':
        from ..deprecations import _warn_deprecated  # noqa: PLC0415
        _warn_deprecated('BBD005', f'{__name__}.SendBytes', 'SendRawBytes')
        return SendRawBytes
    raise AttributeError(f'module {__name__!r} has no attribute {name!r}')
