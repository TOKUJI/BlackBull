"""BlackBull — async ASGI 3.0 web framework.

Public API exports:

- `BlackBull`: the main application object; wraps routing, middleware, and lifespan hooks.
- `serve`: synchronous entry point that runs any ASGI 3.0 callable (also used by the ``blackbull`` console script).
- `Response`, `JSONResponse`, `StreamingResponse`, `WebSocketResponse`: response helpers.
- `cookie_header`: builds a ``Set-Cookie`` header tuple.
- `read_body`: reads and buffers the full request body from the ASGI receive channel.
- `parse_cookies`: parses the ``Cookie`` header into a plain ``dict``.
- `CORS`: adds ``Access-Control-*`` headers; handles preflight OPTIONS requests.
- `as_middleware`: decorator that marks an async function or class as middleware; normalises ``send`` so inner wrappers see only ASGI event dicts.
- `TrustedProxy`: rewrites ``scope['client']`` / ``scope['scheme']`` from proxy headers.
"""
import logging
logging.getLogger('blackbull').addHandler(logging.NullHandler())

from .app import BlackBull, serve
from .request import read_body, parse_cookies
from .response import Response, JSONResponse, StreamingResponse, WebSocketResponse, cookie_header
from .event import Event, EventHandler
from .asgi import ResponseStart, ResponseBody, parse_response_event
from .middleware.cors import CORS
from .middleware.utils import as_middleware
from .middleware.proxy import TrustedProxy
