from .client import Client
from .http1 import (HTTP1Client, HTTP1RequestSender, HTTP1ResponseRecipient,
                    HTTP1UpgradeSession)
from .http2 import ClientResponse, HTTP2Client
from .response import ResponderFactory
# The scenario primitives live in blackbull.fault_injection.
# The names stay reachable from blackbull.client without a deprecation
# warning so existing top-level callers keep working; the deep-import
# path (blackbull.client.scenario) is the one that emits the warning.
from blackbull.fault_injection.scenario_h1 import (
    Abort,
    ReadResponse,
    Scenario,
    ScenarioResult,
    # Re-exported under the deprecated spelling *without* going through the
    # module ``__getattr__`` that warns — this file's own comment promises
    # exactly that, and importing the old name here made the package emit
    # its own deprecation warning on every ``import blackbull.client``.
    SendRawBytes as SendBytes,
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
    'SendBytes',
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
