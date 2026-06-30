"""BlackBull — async ASGI 3.0 web framework.

**Early Alpha** — API may break between MINOR versions; see
``KNOWN_LIMITATIONS.md`` before building production-shape work on top.

Public API exports:

- `BlackBull`: the main application object; wraps routing, middleware, and lifespan hooks.
- `AppConfig`: declarative, immutable holder for the startup settings ``run()`` accepts (port, TLS, workers, …).
- `serve`: synchronous entry point that runs any ASGI 3.0 callable (also used by the ``blackbull`` console script).
- `Response`, `JSONResponse`, `RedirectResponse`, `StreamingResponse`, `EventSourceResponse`, `WebSocketResponse`: response helpers.
- `RouteInfo`: immutable ``(method, path, name)`` snapshot returned by ``app.get_routes()``.
- `Headers`: case-insensitive, ordered, multi-valued HTTP header store.
- `cookie_header`: builds a ``Set-Cookie`` header tuple.
- `read_body`: reads and buffers the full request body from the ASGI receive channel.
- `read_json`: reads the body and parses it as JSON (``None`` on empty/invalid).
- `read_text`: reads the body and decodes it as text.
- `parse_cookies`: parses the ``Cookie`` header into a plain ``dict``.
- `CORS`: adds ``Access-Control-*`` headers; handles preflight OPTIONS requests.
- `as_middleware`: decorator that marks an async function or class as middleware; normalises ``send`` so inner wrappers see only ASGI event dicts.
- `TrustedProxy`: rewrites ``scope['client']`` / ``scope['scheme']`` from proxy headers.

Importing this package does **not** load the server stack
(``blackbull.server.*``).  Use ``ASGIServer`` from ``blackbull.server``
when you want to embed BlackBull's own server; otherwise pass the
``BlackBull`` instance to any external ASGI server (uvicorn, hypercorn,
granian, …) since ``BlackBull.__call__`` is ASGI 3.0 compliant.
"""
import logging
logging.getLogger('blackbull').addHandler(logging.NullHandler())

# Single source of truth for the version is pyproject.toml; expose it at
# runtime via importlib.metadata so the two never drift.  Falls back to a
# sentinel only when blackbull is being imported from a source checkout
# without `pip install -e .` (the test runners and `bench/app.py` both
# install editably, so this path is exercised only in unusual setups).
from importlib.metadata import PackageNotFoundError, version as _pkg_version

try:
    __version__ = _pkg_version('blackbull')
except PackageNotFoundError:
    __version__ = '0.0.0+unknown'

from .app import BlackBull, serve
from .router import RouteInfo
from .config import AppConfig
from .headers import Headers
from .request import read_body, read_json, read_text, parse_cookies
from .response import (
    Response, JSONResponse, RedirectResponse, StreamingResponse,
    EventSourceResponse, WebSocketResponse, cookie_header,
)
from .event import Event, EventHandler
from .asgi import ResponseStart, ResponseBody, parse_response_event
from .middleware.cors import CORS
from .middleware.utils import as_middleware
from .middleware.proxy import TrustedProxy
