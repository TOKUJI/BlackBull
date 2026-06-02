import asyncio
import gzip
from collections.abc import Callable
from ..asgi import ASGIEvent
from ..headers import Headers
from ..asgi import ResponseStart, ResponseBody, parse_response_event
from .utils import as_middleware

_MIN_SIZE = 100  # default minimum body size to bother compressing
_EXECUTOR_THRESHOLD = 65536  # default body size above which compression is offloaded
# Default cap on concurrent executor offloads.  When at the cap, additional
# eligible responses are served *uncompressed* rather than queued — bounded
# fall-back instead of unbounded executor queue growth.  ``0`` disables.
import os as _os  # noqa: PLC0415
_MAX_INFLIGHT = max((_os.cpu_count() or 1) * 2, 4)
_SERVER_PREFERENCE = ['br', 'zstd', 'gzip']  # server-side priority order

# Content-Type prefixes whose payloads are already compressed or binary and
# should not be re-compressed (compressing them wastes CPU with no size gain).
_SKIP_CONTENT_TYPES = (
    'image/',
    'audio/',
    'video/',
    'application/zip',
    'application/gzip',
    'application/x-gzip',
    'application/x-brotli',
    'application/zstd',
    'application/x-zstd',
    'application/pdf',
    'application/wasm',
)


# ---------------------------------------------------------------------------
# Codec detection and selection
# ---------------------------------------------------------------------------

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
    available['gzip'] = gzip.compress
    return available


def _is_compressible_content_type(headers: Headers) -> bool:
    """Return False when the Content-Type signals already-compressed content."""
    ct = headers.get(b'content-type', b'').split(b';')[0].strip().lower()
    ct_str = ct.decode('ascii', errors='ignore')
    return not any(ct_str.startswith(prefix) for prefix in _SKIP_CONTENT_TYPES)


# ---------------------------------------------------------------------------
# Middleware
# ---------------------------------------------------------------------------

@as_middleware
class Compression:
    """ASGI middleware: compress the response body using the best codec the
    client accepts (br > zstd > gzip, in server-preference order).

    Bodies smaller than *min_size* bytes are forwarded uncompressed.
    Responses with already-compressed Content-Types (image/*, video/*, etc.)
    are forwarded uncompressed.
    brotli and zstandard are optional — if not installed the middleware
    falls back gracefully to gzip or no compression.

    BlackBull middleware convention::

        from blackbull.middleware import Compression

        @app.route(path='/', middlewares=[Compression()])
        async def handler(scope, receive, send): ...
    """

    def __init__(self, min_size: int = _MIN_SIZE,
                 executor_threshold: int = _EXECUTOR_THRESHOLD,
                 executor_max_inflight: int = _MAX_INFLIGHT):
        self._min_size = min_size
        self._executor_threshold = executor_threshold
        # Concurrency cap on executor offloads.  When at cap, fall back to
        # uncompressed rather than queueing — keeps the asyncio default
        # thread pool from growing an unbounded backlog under burst load
        # (the HttpArena `static` profile collapse mode, Sprint 29).
        self._executor_max_inflight = executor_max_inflight
        self._executor_inflight: int = 0
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
        if scope.get('type') != 'http':
            await call_next(scope, receive, send)
            return

        headers = scope.get('headers', [])
        if not isinstance(headers, Headers):
            headers = Headers(headers)
        accept = headers.get(b'accept-encoding', b'')
        selection = self._select_codec(accept)
        if selection is None:
            await call_next(scope, receive, send)
            return

        codec_name, compressor = selection
        start_event: dict = {}
        start_forwarded = False
        body_parts: list[bytes] = []
        streaming = False
        skip_compression = False

        async def intercepting_send(event: dict) -> None:
            nonlocal streaming, skip_compression, start_forwarded
            parsed = parse_response_event(event)
            match parsed:
                case ResponseStart():
                    start_event.update(parsed)
                    if not _is_compressible_content_type(parsed.headers):
                        skip_compression = True
                    # An upstream layer (e.g. `StaticFiles` serving a
                    # precompressed sibling) may have already set
                    # Content-Encoding.  Don't double-wrap.
                    elif parsed.headers.get(b'content-encoding'):
                        skip_compression = True
                    # When skipping, forward the start event immediately
                    # so the downstream sender doesn't sit on a body with
                    # no headers (which would be invalid HTTP).
                    if skip_compression:
                        await send(start_event)
                        start_forwarded = True
                case ResponseBody():
                    if streaming or skip_compression:
                        # If we already decided to skip but the start
                        # arrived as part of this body event somehow,
                        # forward it now to be safe.
                        if not start_forwarded and start_event:
                            await send(start_event)
                            start_forwarded = True
                        await send(parsed)
                    elif parsed.more_body:
                        streaming = True
                        await send(start_event)
                        start_forwarded = True
                        if parsed.body:
                            await send({'type': ASGIEvent.HTTP_RESPONSE_BODY,
                                        'body': parsed.body, 'more_body': True})
                    else:
                        body_parts.append(parsed.body)
                case _:
                    await send(parsed)

        await call_next(scope, receive, intercepting_send)

        if streaming:
            return

        # When skip_compression triggered on the upstream ResponseStart,
        # intercepting_send has already forwarded both the start event
        # and the body inline.  Re-sending here would produce two start
        # events on the same response, which the HTTP/1.1 sender treats
        # as the end of the first response — causing the connection to
        # be closed after every successful response.  Detected via the
        # 1:1 success/read-error ratio under wrk keep-alive load
        # (Sprint 29).
        if start_forwarded:
            return

        body = b''.join(body_parts)

        if skip_compression or len(body) < self._min_size:
            await send(start_event)
            await send({'type': ASGIEvent.HTTP_RESPONSE_BODY, 'body': body, 'more_body': False})
            return

        threshold = self._executor_threshold
        if threshold > 0 and len(body) >= threshold:
            # Backpressure: if the executor already has _executor_max_inflight
            # compressions running, skip this one and serve uncompressed
            # rather than queueing.  Prevents the unbounded executor backlog
            # that caused the HttpArena `static` profile to collapse to 0 r/s
            # on run 2 under c=1024 (Sprint 29).  Counter increment / decrement
            # is safe without a lock — asyncio is single-threaded.
            if (self._executor_max_inflight > 0
                    and self._executor_inflight >= self._executor_max_inflight):
                await send(start_event)
                await send({'type': ASGIEvent.HTTP_RESPONSE_BODY,
                            'body': body, 'more_body': False})
                return
            self._executor_inflight += 1
            try:
                loop = asyncio.get_running_loop()
                compressed = await loop.run_in_executor(None, compressor, body)
            finally:
                self._executor_inflight -= 1
        else:
            compressed = compressor(body)
        # The compressed body is a different size; strip any upstream
        # content-length and replace it with the post-compression length.
        # Leaving the original value behind breaks HTTP/1.1 keepalive
        # framing (client expects N bytes but receives the compressed
        # body) and is rejected as a protocol error by strict HTTP/2
        # clients.
        existing = [(k, v) for k, v in start_event.get('headers', [])
                    if k.lower() != b'content-length']
        existing.append((b'content-encoding', codec_name.encode()))
        existing.append((b'content-length', str(len(compressed)).encode()))
        await send({**start_event, 'headers': existing})
        await send({'type': ASGIEvent.HTTP_RESPONSE_BODY, 'body': compressed, 'more_body': False})


def _make_default_compress() -> 'Compression':
    """Build a Compression instance pre-configured from BB_COMPRESSION_*.

    Kept as a module-level helper so the legacy ``from blackbull.middleware
    import compress`` import (which exposes a pre-built instance) keeps
    working through the deprecation alias in :mod:`blackbull.middleware`.
    """
    try:
        from ..env import get_settings as _get_settings  # noqa: PLC0415
        cfg = _get_settings()
        return Compression(
            min_size=cfg.compression_min_size,
            executor_threshold=cfg.compression_executor_threshold,
            executor_max_inflight=cfg.compression_max_inflight,
        )
    except Exception:
        return Compression()
