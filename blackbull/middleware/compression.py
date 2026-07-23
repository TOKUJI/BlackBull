import asyncio
import functools
import gzip
from collections.abc import Callable
from ..asgi import ASGIEvent
from ..connection import Connection
from ..headers import Headers
from ..asgi import ResponseStart, ResponseBody, parse_response_event
from ..server.cap_log import log_cap_hit
from .utils import as_middleware

_MIN_SIZE = 100  # default minimum body size to bother compressing
_EXECUTOR_THRESHOLD = 65536  # default body size above which compression is offloaded
# Default brotli quality level for dynamic responses.  The brotli library's
# own default is 11 (max compression, designed for build-time / static
# pre-compression) — for sub-KB dynamic JSON that's ~5–15 ms of CPU per
# response, which pegs the loop.  4 matches Google's and Cloudflare's
# recommendation for dynamic content; 5 matches Apache mod_brotli;
# 6 matches nginx ngx_brotli.  Configurable via ``BB_BROTLI_QUALITY``.
_BROTLI_QUALITY = 4
# Default cap on concurrent executor offloads.  When at the cap, additional
# eligible responses are served *uncompressed* rather than queued — bounded
# fall-back instead of unbounded executor queue growth.  ``0`` disables.
import os as _os  # noqa: PLC0415
_MAX_INFLIGHT = max((_os.cpu_count() or 1) * 2, 4)
_SERVER_PREFERENCE = ['br', 'zstd', 'gzip']  # server-side priority order

# Content-Type prefixes whose payloads are already compressed or binary and
# should not be re-compressed (compressing them wastes CPU with no size gain).
#
# ``font/woff`` and ``font/woff2`` are intentionally listed but ``font/``
# is not blanket-skipped: ``font/ttf``, ``font/otf``, and ``font/sfnt`` are
# uncompressed font tables that DO benefit from gzip/brotli, so they stay
# off this list and run through the codec like any other text-shaped
# payload.  WOFF wraps zlib internally; WOFF2 wraps brotli internally —
# re-compressing them is the worst case (high-entropy input; under the
# brotli library's bare-call default of quality 11 — BlackBull's own
# default is q=4 via ``BB_BROTLI_QUALITY``) and contributes a measurable
# per-request CPU tail in Sprint 35's HttpArena static-rotate probe.
_SKIP_CONTENT_TYPES = (
    'image/',
    'audio/',
    'video/',
    'font/woff',
    'font/woff2',
    'application/font-woff',
    'application/font-woff2',
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

def _detect_codecs(brotli_quality: int = _BROTLI_QUALITY) -> dict[str, Callable[[bytes], bytes]]:
    """Return a dict of codec-name → compress-callable for every available encoder.

    ``brotli_quality`` is bound into the ``br`` callable so each request
    pays only the dict lookup + call, with no per-call kwarg setup.
    """
    available: dict[str, Callable[[bytes], bytes]] = {}
    try:
        import brotli  # type: ignore[import-untyped]
        available['br'] = functools.partial(brotli.compress, quality=brotli_quality)
    except ImportError:
        pass  # brotli not installed → 'br' codec unavailable.
    try:
        import zstandard  # type: ignore[import-untyped]
        cctx = zstandard.ZstdCompressor()
        available['zstd'] = cctx.compress
    except ImportError:
        pass  # zstandard not installed → 'zstd' codec unavailable.
    available['gzip'] = gzip.compress
    return available


def _is_compressible_content_type(headers: Headers) -> bool:
    """Return False when the Content-Type signals already-compressed content."""
    ct = headers.get(b'content-type', b'').split(b';')[0].strip().lower()
    ct_str = ct.decode('ascii', errors='ignore')
    return not any(ct_str.startswith(prefix) for prefix in _SKIP_CONTENT_TYPES)


def _merge_vary(headers: list[tuple[bytes, bytes]],
                field: bytes = b'Accept-Encoding') -> None:
    """Ensure the response ``Vary`` header lists *field* (RFC 9110 §12.5.5).

    A compressed response's body depends on the request ``Accept-Encoding``;
    without ``Vary: Accept-Encoding`` a shared cache may replay the encoded
    body to a client that sent ``identity``/no ``Accept-Encoding`` (bug 1.21a).
    Folds *field* into an existing ``Vary`` (no duplicate token; a pre-existing
    ``Vary: *`` already covers everything and is left untouched); otherwise
    appends ``Vary: Accept-Encoding``.  Mutates *headers* in place.
    """
    field_l = field.lower()
    for i, (k, v) in enumerate(headers):
        if k.lower() == b'vary':
            tokens = [t.strip().lower() for t in v.split(b',')]
            if b'*' in tokens or field_l in tokens:
                return
            headers[i] = (k, v + b', ' + field)
            return
    headers.append((b'vary', field))


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
        async def handler(conn, receive, send): ...
    """

    def __init__(self, min_size: int = _MIN_SIZE,
                 executor_threshold: int = _EXECUTOR_THRESHOLD,
                 executor_max_inflight: int = _MAX_INFLIGHT,
                 brotli_quality: int = _BROTLI_QUALITY):
        self._min_size = min_size
        self._executor_threshold = executor_threshold
        # Concurrency cap on executor offloads.  When at cap, fall back to
        # uncompressed rather than queueing — keeps the asyncio default
        # thread pool from growing an unbounded backlog under burst load
        # (the HttpArena `static` profile collapse mode, Sprint 29).
        self._executor_max_inflight = executor_max_inflight
        self._executor_inflight: int = 0
        self._brotli_quality = brotli_quality
        self._available = _detect_codecs(brotli_quality=brotli_quality)
        # ``Accept-Encoding`` header bytes → selection.  Real-world traffic
        # has very few distinct Accept-Encoding values (browsers send a
        # constant string; benchmark generators send one); parsing the
        # q-values + iterating the server-preference list on every request
        # showed up in the Sprint 33 py-spy profile.  Bounded so a hostile
        # peer can't grow it unboundedly.
        self._codec_cache: dict[bytes, tuple[str, Callable[[bytes], bytes]] | None] = {}

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
                        pass  # malformed q-value → keep the default quality.
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
        cache = self._codec_cache
        if accept_header in cache:
            return cache[accept_header]
        accepted = set(self._parse_accept_encoding(accept_header))
        result: tuple[str, Callable[[bytes], bytes]] | None = None
        for codec in _SERVER_PREFERENCE:
            if codec in accepted and codec in self._available:
                result = (codec, self._available[codec])
                break
        if len(cache) < 256:
            cache[accept_header] = result
        return result

    @staticmethod
    def _vary_ensuring_send(send):
        """Wrap *send* so a compressible, not-yet-encoded ``ResponseStart`` gains
        ``Vary: Accept-Encoding`` — used on the no-matching-codec path where the
        body is forwarded verbatim but must still be cache-keyed on the encoding
        (bug 1.21f, Branch 1).  Same predicate as the compress path's decision
        point: compressible Content-Type AND no pre-existing Content-Encoding.
        """
        async def vary_send(event: dict) -> None:
            parsed = parse_response_event(event)
            if isinstance(parsed, ResponseStart) and \
                    _is_compressible_content_type(parsed.headers) and \
                    not parsed.headers.get(b'content-encoding'):
                hdrs = list(event.get('headers', []))
                _merge_vary(hdrs)
                event = {**event, 'headers': hdrs}
            await send(event)
        return vary_send

    async def __call__(self, conn, receive, send, call_next):
        # Native Connection for HTTP and WebSocket; the guard is defensive
        # against a raw ASGI scope dict (only reachable outside BlackBull's own
        # dispatch).
        if not isinstance(conn, Connection):
            await call_next(conn, receive, send)
            return

        accept = conn.headers.get(b'accept-encoding', b'')
        selection = self._select_codec(accept)
        if selection is None:
            # No codec the client accepts (e.g. no/identity Accept-Encoding).
            # We won't compress, but the response may still be *compressible*,
            # so a downstream shared cache needs Vary: Accept-Encoding — else it
            # stores this identity variant under the bare key and replays it to a
            # later client that does accept an encoding (bug 1.21f, Branch 1).
            await call_next(conn, receive, self._vary_ensuring_send(send))
            return

        codec_name, compressor = selection
        start_event: dict = {}
        start_forwarded = False
        body_parts: list[bytes] = []
        streaming = False
        skip_compression = False

        async def intercepting_send(event: dict) -> None:
            nonlocal streaming, skip_compression, start_forwarded
            # Fast path: once the start event has been forwarded under a
            # pass-through decision (already-encoded response, non-
            # compressible Content-Type, or streaming chunks), subsequent
            # events are forwarded verbatim — no parse, no re-wrap, no
            # match.  Sprint 33 py-spy showed this overhead at ~35 % of
            # the static-path CPU on responses StaticFiles already
            # encoded via a precompressed sibling.
            if start_forwarded and (skip_compression or streaming):
                await send(event)
                return
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
                    else:
                        # Compressible + not pre-encoded: this response's body
                        # varies by Accept-Encoding on *every* exit path
                        # (compressed, too-small, executor-at-cap), so stamp
                        # Vary now — at the decision point — instead of only
                        # after a successful compress (bug 1.21f).  Later paths
                        # inherit it via start_event; the compress path's own
                        # _merge_vary then no-ops.
                        hdrs = list(start_event.get('headers', []))
                        _merge_vary(hdrs)
                        start_event['headers'] = hdrs
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

        await call_next(conn, receive, intercepting_send)

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
                log_cap_hit('compression_max_inflight',
                            requested=self._executor_inflight + 1,
                            limit=self._executor_max_inflight,
                            protocol='compression')
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
        # Shared caches must key this response on Accept-Encoding.
        _merge_vary(existing)
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
            brotli_quality=cfg.brotli_quality,
        )
    except Exception:
        return Compression()
