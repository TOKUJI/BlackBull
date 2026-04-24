import gzip as _gzip
from collections.abc import Callable
from ..server.headers import Headers

_MIN_SIZE = 100  # default minimum body size to bother compressing
_SERVER_PREFERENCE = ['br', 'zstd', 'gzip']  # server-side priority order


def _detect_codecs() -> dict[str, Callable[[bytes], bytes]]:
    """Return a dict of codec-name → compress-callable for every available encoder."""
    available: dict[str, Callable[[bytes], bytes]] = {}
    try:
        import brotli  # type: ignore[import-untyped]
        available['br'] = brotli.compress
    except ImportError:
        pass
    try:
        import zstandard  # type: ignore[import-untyped]
        cctx = zstandard.ZstdCompressor()
        available['zstd'] = cctx.compress
    except ImportError:
        pass
    available['gzip'] = _gzip.compress
    return available


class CompressionMiddleware:
    """ASGI middleware: compress the response body using the best codec the
    client accepts (br > zstd > gzip, in server-preference order).

    Bodies smaller than *min_size* bytes are forwarded uncompressed.
    brotli and zstandard are optional — if not installed the middleware
    falls back gracefully to gzip or no compression.

    BlackBull middleware convention::

        compress = CompressionMiddleware()

        @app.route(path='/', middlewares=[compress])
        async def handler(scope, receive, send): ...
    """

    def __init__(self, min_size: int = _MIN_SIZE):
        self._min_size = min_size
        self._available = _detect_codecs()

    @staticmethod
    def _parse_accept_encoding(header: bytes) -> list[str]:
        """Parse Accept-Encoding and return codec names sorted by descending q-value.

        Example: b'br;q=1.0, gzip;q=0.8' → ['br', 'gzip']
        """
        result: list[tuple[float, str]] = []
        for token in header.split(b','):
            parts = token.strip().split(b';')
            name = parts[0].strip().lower().decode('ascii', errors='ignore')
            q = 1.0
            for param in parts[1:]:
                param = param.strip()
                if param.startswith(b'q='):
                    try:
                        q = float(param[2:])
                    except ValueError:
                        pass
            if name:
                result.append((q, name))
        result.sort(key=lambda x: x[0], reverse=True)
        return [name for _, name in result]

    def _select_codec(self, accept_header: bytes) -> tuple[str, Callable[[bytes], bytes]] | None:
        """Pick the best codec that the client accepts and the server has installed.

        Server preference order (br > zstd > gzip) is applied among the
        codecs the client lists, regardless of their q-values, because the
        server knows which codec yields better compression.
        Returns ``None`` when there is no overlap.
        """
        accepted = set(self._parse_accept_encoding(accept_header))
        for codec in _SERVER_PREFERENCE:
            if codec in accepted and codec in self._available:
                return codec, self._available[codec]
        return None

    async def __call__(self, scope, receive, send, call_next):
        headers = scope.get('headers', [])
        if not isinstance(headers, Headers):
            headers = Headers(headers)

        accept = headers.get(b'accept-encoding', b'')
        selection = self._select_codec(accept)

        if selection is None:
            await call_next(scope, receive, send)
            return

        codec_name, compressor = selection

        # Buffer the response so we can compress the body and update headers.
        start_event: dict = {}
        body_parts: list[bytes] = []

        async def buffering_send(event: dict) -> None:
            if event.get('type') == 'http.response.start':
                start_event.update(event)
            elif event.get('type') == 'http.response.body':
                body_parts.append(event.get('body', b''))

        await call_next(scope, receive, buffering_send)

        body = b''.join(body_parts)
        if len(body) < self._min_size:
            await send(start_event)
            await send({'type': 'http.response.body', 'body': body, 'more_body': False})
            return

        compressed = compressor(body)
        existing = list(start_event.get('headers', []))
        existing.append((b'content-encoding', codec_name.encode()))
        await send({**start_event, 'headers': existing})
        await send({'type': 'http.response.body', 'body': compressed, 'more_body': False})


# Default instance — used as a plain middleware function
compress = CompressionMiddleware()
