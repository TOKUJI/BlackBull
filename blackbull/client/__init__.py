from .client import Client
from .http1 import HTTP1Client, HTTP1RequestSender, HTTP1ResponseRecipient
from .http2 import ClientResponse, HTTP2Client
from .response import ResponderFactory
# Sprint 46 moved the scenario primitives to blackbull.fault_injection.
# The names stay reachable from blackbull.client without a deprecation
# warning so existing top-level callers keep working; the deep-import
# path (blackbull.client.scenario) is the one that emits the warning.
from blackbull.fault_injection.scenario_h1 import (
    Abort,
    ReadResponse,
    Scenario,
    ScenarioResult,
    SendBytes,
    Sleep,
    Step,
)
from .websocket import WebSocketClient, WebSocketSession
from .websocket_h2 import WebSocketH2Client, WebSocketH2Session
from .exceptions import (
    ClientError,
    ConnectionError,
    HandshakeError,
    ProtocolError,
    StreamReset,
)

__all__ = [
    'Abort',
    'Client',
    'ClientResponse',
    'HTTP1Client',
    'HTTP1RequestSender',
    'HTTP1ResponseRecipient',
    'HTTP2Client',
    'ReadResponse',
    'ResponderFactory',
    'Scenario',
    'ScenarioResult',
    'SendBytes',
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
    'StreamReset',
]
