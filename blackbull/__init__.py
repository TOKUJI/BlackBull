"""BlackBull public API. MINOR releases may change contracts.

Importing blackbull loads the server stack. Use blackbull.server.ASGIServer
to embed it, or pass a BlackBull instance to an external ASGI host.
Request is a deprecated Connection alias; new code should use Connection.
"""
# Keep implementation imports private; __all__ alone does not hide package attributes.
import logging as _logging
_logging.getLogger('blackbull').addHandler(_logging.NullHandler())

# Runtime version comes from installed metadata; a source-only checkout
# without an install uses the sentinel.
from importlib.metadata import (PackageNotFoundError as _PackageNotFoundError,
                                version as _pkg_version)

try:
    __version__ = _pkg_version('blackbull')
except _PackageNotFoundError:
    __version__ = '0.0.0+unknown'

from .app import BlackBull, serve
from .di import Depends
from .router import RouteInfo, HTTPException, UnprocessableQuery, QUERY
from .config import AppConfig
from .headers import Headers
from .request import (
    read_body, read_json, read_text, parse_cookies, cookies_from_headers,
    ClientDisconnected)
from .connection import Connection
from .websocket import WebSocket, WebSocketDisconnect
from .response import (
    Response, JSONResponse, RedirectResponse, StreamingResponse,
    EventSourceResponse, WebSocketResponse, cookie_header,
)
from .event import Event, EventHandler
from .asgi import (
    ResponseStart, ResponseBody, parse_response_event,
    ASGIReceiveEvent, ASGISendEvent, ASGIReceiveCallable, ASGISendCallable,
)
from .middleware.cors import CORS
from .middleware.utils import as_middleware
from .middleware.proxy import TrustedProxy

# Exclude Request from __all__ so import * cannot trigger its deprecation warning.
from .server.listener import InheritedFd, Listener, Tcp, Unix

__all__ = [
    # application + entry points
    'BlackBull', 'serve', 'AppConfig',
    # routing
    'RouteInfo', 'HTTPException', 'UnprocessableQuery', 'QUERY',
    # request side
    'Connection', 'Headers', 'read_body', 'read_json', 'read_text',
    'parse_cookies', 'cookies_from_headers', 'ClientDisconnected',
    # response side
    'Response', 'JSONResponse', 'RedirectResponse', 'StreamingResponse',
    'EventSourceResponse', 'WebSocketResponse', 'cookie_header',
    # websocket
    'WebSocket', 'WebSocketDisconnect',
    # dependency injection
    'Depends',
    # events
    'Event', 'EventHandler',
    # ASGI interop
    'ResponseStart', 'ResponseBody', 'parse_response_event',
    'ASGIReceiveEvent', 'ASGISendEvent',
    'ASGIReceiveCallable', 'ASGISendCallable',
    # bundled middleware
    'CORS', 'as_middleware', 'TrustedProxy',
    # the sockets a deployment asks for
    'Listener', 'Tcp', 'Unix', 'InheritedFd',
]


def __getattr__(name):
    """Deprecated Request alias for Connection; removal no earlier than 2027-08-01.
    """
    if name == 'Request':
        import warnings
        warnings.warn(
            'blackbull.Request is deprecated — use blackbull.Connection instead. '
            "Replace `request: Request` with `conn: Connection` in handler "
            'signatures. The API is identical: conn.method, conn.path, '
            'conn.headers, conn.cookies, conn.query, conn.query_list, '
            'conn.path_params, conn.body(), conn.json(), conn.text(), '
            'conn.form(). Request will be removed no earlier than 2027-08-01.',
            DeprecationWarning, stacklevel=2)
        return Connection
    raise AttributeError(f'module {__name__!r} has no attribute {name!r}')
