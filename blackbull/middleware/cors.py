"""CORS response headers and preflight handling.

CORS is browser enforcement, not server authorization. Unallowed origins
pass through without granting cross-origin access.
"""
from ..native import NativeResponse
from ..connection import Connection
from ..headers import Headers, _MinimalResponseHeaders, _as_response_fields
from .utils import as_middleware


@as_middleware
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
        """Build the middleware.

        ``allow_credentials=True`` cannot be combined with
        ``allow_origins=['*']``: the pair raises ``ValueError``.  A wildcard
        origin with credentials is what the CORS specification forbids, not
        something this middleware chooses to refuse.
        """
        if isinstance(allow_origins, str):
            allow_origins = [allow_origins]
        if isinstance(allow_headers, str):
            allow_headers = [allow_headers]
        if allow_credentials and '*' in allow_origins:
            raise ValueError(
                "allow_credentials=True cannot be combined with allow_origins=['*']. "
                "List explicit origins instead."
            )
        self._origins = set(allow_origins)
        self._wildcard = '*' in allow_origins
        self._reflect_headers = allow_credentials and allow_headers == ['*']
        fields = []
        if allow_credentials:
            fields.append((b'access-control-allow-credentials', b'true'))
        if exposed := ','.join(expose_headers or []).encode():
            fields.append((b'access-control-expose-headers', exposed))
        if not self._wildcard:
            fields.append((b'vary', b'Origin'))
        self._cors_fields = _as_response_fields(fields)
        preflight = self._cors_fields.copy()
        preflight.add(b'access-control-allow-methods',
                      ','.join(allow_methods or ['GET', 'POST', 'HEAD', 'OPTIONS']).encode())
        if self._reflect_headers:
            preflight.discard(b'vary')
            preflight.add(b'vary', b'Origin, Access-Control-Request-Headers')
        elif allowed := ','.join(allow_headers).encode():
            preflight.add(b'access-control-allow-headers', allowed)
        if max_age is not None:
            preflight.add(b'access-control-max-age', str(max_age).encode())
        self._preflight_fields = preflight

    def _is_allowed(self, origin: str) -> bool:
        return self._wildcard or origin in self._origins

    def _cors_headers(self, origin: bytes,
                      template: _MinimalResponseHeaders) -> _MinimalResponseHeaders:
        hdrs = template.copy()
        hdrs.add(b'access-control-allow-origin', b'*' if self._wildcard else origin)
        return hdrs

    def _preflight_headers(self, origin: bytes, headers: Headers) -> _MinimalResponseHeaders:
        """Build the CORS headers for this preflight."""
        hdrs = self._cors_headers(origin, self._preflight_fields)
        if self._reflect_headers:
            allowed = headers.get_combined(b'access-control-request-headers')
            if allowed:
                hdrs.add(b'access-control-allow-headers', allowed)
        return hdrs

    @staticmethod
    def _injecting_send(send, cors_hdrs):
        """Wrap *send* so the response's header arm gains *cors_hdrs*.

        ASGI headers are copied at the native conversion boundary before
        this wrapper mutates them.
        """
        # Unannotated: rebuilt per request.
        async def cors_send(event):
            if (isinstance(event, NativeResponse)
                    and (event._extension is None or event.push is None)):
                if event._header is not None:
                    event._header.extend(cors_hdrs)
            await send(event)

        return cors_send

    async def __call__(self, conn, receive, send, call_next) -> None:
        if not isinstance(conn, Connection):
            await call_next(conn, receive, send)
            return

        headers = conn.headers

        origin = headers.get(b'origin', b'')
        if not origin or not self._is_allowed(origin.decode()):
            await call_next(conn, receive, send)
            return

        # Preflight: respond directly, never call call_next
        if conn.method == 'OPTIONS' and headers.get(b'access-control-request-method'):
            # Framework-owned producer: emits native, like every other
            # response BlackBull generates itself.  One object, one send.
            await send(NativeResponse(status=200,
                                      header=self._preflight_headers(origin, headers),
                                      body=b''))
            return

        # Actual cross-origin request: inject CORS headers into the response start event
        cors_hdrs = self._cors_headers(origin, self._cors_fields)
        await call_next(conn, receive, self._injecting_send(send, cors_hdrs))
