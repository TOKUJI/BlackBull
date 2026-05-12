from ..server.constants import ASGIEvent
from ..server.headers import Headers
from .utils import _normalize_send


class CORS:
    """Cross-Origin Resource Sharing (CORS) middleware.

    Handles preflight OPTIONS requests and attaches CORS headers to actual
    cross-origin responses.  Requests without an ``Origin`` header pass through
    unchanged.

    Args:
        allow_origins: Explicit origin strings or ``['*']`` for wildcard.
        allow_methods: HTTP methods permitted in cross-origin requests.
            Defaults to ``['GET', 'POST', 'HEAD', 'OPTIONS']``.
        allow_headers: Request headers permitted; ``['*']`` allows all.
        allow_credentials: Emit ``Access-Control-Allow-Credentials: true``.
            Cannot be combined with ``allow_origins=['*']``.
        expose_headers: Response headers the browser JS may read.
        max_age: Preflight cache lifetime in seconds.  ``None`` omits the header.

    Usage::

        app = BlackBull()
        app.use(CORS(
            allow_origins=['https://myapp.example.com'],
            allow_credentials=True,
            max_age=3600,
        ))
    """

    def __init__(
        self,
        allow_origins: list[str] | str = '*',
        allow_methods: list[str] | None = None,
        allow_headers: list[str] | str = '*',
        allow_credentials: bool = False,
        expose_headers: list[str] | None = None,
        max_age: int | None = 600,
    ) -> None:
        if isinstance(allow_origins, str):
            allow_origins = [allow_origins]
        if isinstance(allow_headers, str):
            allow_headers = [allow_headers]
        if allow_credentials and '*' in allow_origins:
            raise ValueError(
                "allow_credentials=True cannot be combined with allow_origins=['*']. "
                "List explicit origins instead."
            )
        self._origins     = set(allow_origins)
        self._wildcard    = '*' in allow_origins
        self._methods     = ','.join(allow_methods or ['GET', 'POST', 'HEAD', 'OPTIONS'])
        self._allow_hdrs  = ','.join(allow_headers) if allow_headers != ['*'] else '*'
        self._credentials = allow_credentials
        self._expose      = ','.join(expose_headers or [])
        self._max_age     = str(max_age) if max_age is not None else None

    def _is_allowed(self, origin: str) -> bool:
        return self._wildcard or origin in self._origins

    def _cors_headers(self, origin: str) -> list[tuple[bytes, bytes]]:
        hdrs = [(b'access-control-allow-origin',
                 b'*' if self._wildcard else origin.encode())]
        if self._credentials:
            hdrs.append((b'access-control-allow-credentials', b'true'))
        if self._expose:
            hdrs.append((b'access-control-expose-headers', self._expose.encode()))
        if not self._wildcard:
            # Vary: Origin tells caches that the response differs by origin
            hdrs.append((b'vary', b'Origin'))
        return hdrs

    async def __call__(self, scope, receive, send, call_next) -> None:
        if scope.get('type') != 'http':
            await call_next(scope, receive, send)
            return

        headers = scope.get('headers', [])
        if not isinstance(headers, Headers):
            headers = Headers(headers)

        origin = headers.get(b'origin', b'').decode()
        if not origin or not self._is_allowed(origin):
            await call_next(scope, receive, send)
            return

        # Preflight: respond directly, never call call_next
        acr_method = headers.get(b'access-control-request-method', b'')
        if scope.get('method') == 'OPTIONS' and acr_method:
            cors_hdrs = self._cors_headers(origin)
            cors_hdrs.append((b'access-control-allow-methods', self._methods.encode()))
            cors_hdrs.append((b'access-control-allow-headers', self._allow_hdrs.encode()))
            if self._max_age:
                cors_hdrs.append((b'access-control-max-age', self._max_age.encode()))
            await send({'type': ASGIEvent.HTTP_RESPONSE_START, 'status': 200, 'headers': cors_hdrs})
            await send({'type': ASGIEvent.HTTP_RESPONSE_BODY, 'body': b'', 'more_body': False})
            return

        # Actual cross-origin request: inject CORS headers into the response start event
        cors_hdrs = self._cors_headers(origin)

        async def cors_send(event):
            if isinstance(event, dict) and event.get('type') == ASGIEvent.HTTP_RESPONSE_START:
                existing = list(event.get('headers', []))
                existing.extend(cors_hdrs)
                event = {**event, 'headers': existing}
            await send(event)

        await call_next(scope, receive, _normalize_send(cors_send))
