import asyncio
import functools
import gzip
from collections.abc import Callable
from ..asgi import ASGIEvent
from ..connection import Connection
from ..headers import Headers
from ..native import NativeResponse
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
# per-request CPU tail on a static-asset workload.
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
    body to a client that sent ``identity``/no ``Accept-Encoding``.
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


def _stamp_vary_if_compressible(header: list[tuple[bytes, bytes]]) -> bool:
    """Whether *header* describes a body worth compressing; stamps ``Vary``.

    The decision point shared by every native exit: a compressible
    Content-Type that is not already encoded is a compression candidate, and
    its body varies by ``Accept-Encoding`` on *all* outcomes — compressed,
    too small, executor at cap, or handed to ``sendfile`` — so ``Vary`` is
    stamped here rather than only where compression succeeds.  Mutates
    *header* in place (zero-copy; the caller owns the list).
    """
    if not _is_compressible_content_type(Headers(header)):
        return False
    if any(k.lower() == b'content-encoding' for k, _ in header):
        return False
    _merge_vary(header)
    return True


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
        # (the collapse mode a static-asset workload triggers).
        self._executor_max_inflight = executor_max_inflight
        self._executor_inflight: int = 0
        self._available = _detect_codecs(brotli_quality=brotli_quality)
        # ``Accept-Encoding`` header bytes → selection.  Real-world traffic
        # has very few distinct Accept-Encoding values (browsers send a
        # constant string; benchmark generators send one); parsing the
        # q-values + iterating the server-preference list on every request
        # showed up in py-spy profiles.  Bounded so a hostile
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

    async def _compress(self, compressor: Callable[[bytes], bytes],
                        body: bytes) -> bytes | None:
        """Offload *body*'s compression to the executor, honouring the
        in-flight cap.  Only called when the caller's threshold check says
        the body crosses ``_executor_threshold`` (below it the caller inlines
        the synchronous ``compressor(body)`` — no coroutine hop on the
        common small-body path).  Returns ``None`` when the executor is at
        cap (the caller serves the body uncompressed).  Shared by the native
        complete-response path and the ``_dict_event`` lane so the
        backpressure behaviour is defined once.
        """
        # Backpressure: if the executor already has _executor_max_inflight
        # compressions running, skip this one and serve uncompressed rather
        # than queueing.  Prevents the unbounded executor backlog that caused
        # the HttpArena `static` profile to collapse to 0 r/s on run 2 under
        # c=1024.  Counter increment / decrement is safe without a lock —
        # asyncio is single-threaded.
        if (self._executor_max_inflight > 0
                and self._executor_inflight >= self._executor_max_inflight):
            log_cap_hit('compression_max_inflight',
                        requested=self._executor_inflight + 1,
                        limit=self._executor_max_inflight,
                        protocol='compression')
            return None
        self._executor_inflight += 1
        try:
            loop = asyncio.get_running_loop()
            return await loop.run_in_executor(None, compressor, body)
        finally:
            self._executor_inflight -= 1

    @staticmethod
    def _vary_ensuring_send(send):
        """Wrap *send* so a compressible, not-yet-encoded ``ResponseStart`` gains
        ``Vary: Accept-Encoding`` — used on the no-matching-codec path where the
        body is forwarded verbatim but must still be cache-keyed on the encoding
        Same predicate as the compress path's decision
        point: compressible Content-Type AND no pre-existing Content-Encoding.
        """
        # Unannotated on purpose: rebuilt per request (see _wrap_send in
        # app.py).  ``event`` is a NativeResponse or an ASGISendEvent.  The
        # import lives at per-request scope — inside the per-event closure it
        # would re-bind for every chunk of a streamed response.

        async def vary_send(event):
            # H1 native path: the header arm is a NativeResponse — stamp Vary
            # directly on its header list (zero-copy; no expansion).  Absence
            # is ``is not None`` — never truthiness.
            if isinstance(event, NativeResponse):
                if event._header is not None:
                    headers = Headers(event._header)
                    if _is_compressible_content_type(headers) and \
                            not headers.get(b'content-encoding'):
                        _merge_vary(event._header)
            # Discriminate on the raw type before building anything.  Going
            # through `parse_response_event` allocated a `ResponseBody` copy
            # of every body event just to have the next line's `isinstance`
            # reject it — a per-chunk cost on a streamed response, for a
            # wrapper that only ever cares about the start event.
            elif isinstance(event, dict) and \
                    event.get('type') == ASGIEvent.HTTP_RESPONSE_START:
                headers = Headers(event.get('headers', []))
                if _is_compressible_content_type(headers) and \
                        not headers.get(b'content-encoding'):
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
            # later client that does accept an encoding.
            await call_next(conn, receive, self._vary_ensuring_send(send))
            return

        codec_name, compressor = selection
        start_forwarded = False
        streaming = False
        skip_compression = False
        # A header-arm NativeResponse awaiting its body (the StaticFiles
        # shape).  Held, never expanded, so the pair can be merged back into
        # one object at the decision point.
        pending_header = None

        # Unannotated on purpose: rebuilt per request (see _wrap_send in
        # app.py).  ``event`` is a NativeResponse or an ASGISendEvent.  The
        # import lives at per-request scope — inside the per-event closure it
        # would re-bind for every chunk of a streamed response.

        async def _emit_native_complete(status, header, body,
                                        original=None) -> None:
            """Decide, compress, and emit the response as **one** object.

            The whole point of the native lane: no ``to_asgi()`` expansion
            into dicts for the layer below to convert straight back.  Pass
            *original* when the caller already holds an equivalent
            ``NativeResponse``, so the uncompressed exit forwards it verbatim
            instead of allocating a copy.
            """
            if _stamp_vary_if_compressible(header) and len(body) >= self._min_size:
                threshold = self._executor_threshold
                if threshold > 0 and len(body) >= threshold:
                    compressed = await self._compress(compressor, body)
                else:
                    # Below the offload threshold: compress synchronously on
                    # the loop — no coroutine hop on the common small-body
                    # (json-comp) range.
                    compressed = compressor(body)
                if compressed is not None:
                    # The compressed body is a different size; strip any
                    # upstream content-length and replace it with the
                    # post-compression length (keeps H1 keepalive framing and
                    # strict H2 clients correct).
                    existing = [(k, v) for k, v in header
                                if k.lower() != b'content-length']
                    existing.append(
                        (b'content-encoding', codec_name.encode()))
                    existing.append(
                        (b'content-length', str(len(compressed)).encode()))
                    _merge_vary(existing)
                    await send(NativeResponse(status=status, header=existing,
                                              body=compressed))
                    return
            # Uncompressed forward: pre-encoded / non-compressible / too-small
            # / executor-at-cap.  Vary is already stamped on *header* when this
            # response was a candidate, so either way the object carries the
            # correct cache key.
            await send(original if original is not None else NativeResponse(
                status=status, header=header, body=body))

        async def _release_pending(held) -> None:
            """Forward a held header arm verbatim and stop compressing.

            Used when whatever followed the header is something compression
            cannot act on — a ``pathsend`` (we never see the bytes), or a
            streamed chunk (we no longer have the body in one piece).  The
            header has to go out *first*: the sender drops a pathsend it has
            no buffered start for, which left a large static file answering
            with no response at all.
            """
            nonlocal start_forwarded, skip_compression
            _stamp_vary_if_compressible(held._header)
            await send(held)
            start_forwarded = True
            skip_compression = True

        async def intercepting_send(event):
            nonlocal streaming, skip_compression, start_forwarded
            nonlocal pending_header
            # H1/H2 native path.  Two shapes reach the one-object fast path:
            # a *complete* NativeResponse (header + terminal body together,
            # the shape a handler returning a ``Response`` produces), and a
            # header arm followed by its terminal body — the shape
            # ``StaticFiles`` produces, which is held here and merged.
            # Expanding either through ``to_asgi()`` → dict →
            # ``wrap_native_send`` → NativeResponse round-trips the exact
            # two-dicts-two-sends cost the native seam removed (measured
            # against v0.67.0 on m7a.8xlarge: static −3.4〜−6.3 %, json-comp
            # −1.2〜−3.2 %).  Trailer shapes and plain dict events keep the
            # ``_dict_event`` lane.
            if isinstance(event, NativeResponse):
                # Pass-through: a forward-verbatim decision is already made,
                # so later objects are relayed untouched (mirrors the
                # ``_dict_event`` fast path).
                if start_forwarded and (skip_compression or streaming):
                    await send(event)
                    return

                held, pending_header = pending_header, None
                if held is not None:
                    if (event._header is None and event._body is not None
                            and not event.more_body
                            and not event.expects_trailers
                            and event.trailers is None):
                        # The terminal body for the held header: the two
                        # halves are a complete response again.
                        await _emit_native_complete(
                            held.status, held._header, event._body)
                        start_forwarded = True
                        return
                    # A streamed chunk, trailers, or a second header — give up
                    # on compressing and relay both in order.
                    await _release_pending(held)
                    await send(event)
                    return

                if (not streaming and not skip_compression
                        and not start_forwarded
                        and event._header is not None
                        and event._body is not None
                        and not event.more_body
                        and not event.expects_trailers
                        and event.trailers is None):
                    await _emit_native_complete(
                        event.status, event._header, event._body,
                        original=event)
                    start_forwarded = True
                    return

                if (not streaming and not skip_compression
                        and not start_forwarded
                        and event._header is not None
                        and event._body is None
                        and not event.expects_trailers
                        and event.trailers is None):
                    # Header arm alone.  Hold it — the body that follows
                    # completes the response, and the compress decision needs
                    # both.  Nothing is on the wire yet, so holding costs no
                    # ordering; the tail releases it if no body ever arrives.
                    pending_header = event
                    return

                # Every remaining native shape — a sendfile form, a
                # trailer-bearing response, a streaming chunk with no held
                # header.  None can be compressed: we either never see the
                # bytes (sendfile) or no longer hold them in one piece.
                # Decide once, then relay verbatim.
                if event._header is not None:
                    _stamp_vary_if_compressible(event._header)
                    start_forwarded = True
                skip_compression = True
                await send(event)
                return

            # A plain dict — ``push``, or an event the native seam does not
            # model.  Uncompressible for the same reason; release a held
            # header first so the sender has its headers before the thing that
            # depends on them.
            if pending_header is not None:
                held, pending_header = pending_header, None
                await _release_pending(held)
            skip_compression = True
            await send(event)

        await call_next(conn, receive, intercepting_send)

        if pending_header is not None:
            # A header arm with no body event behind it (a handler that sent
            # headers and stopped).  Release it rather than swallow the
            # response.
            held, pending_header = pending_header, None
            _stamp_vary_if_compressible(held._header)
            await send(held)
            return

        # Every other path already emitted its response inside
        # ``intercepting_send`` — the native seam decides and sends in one
        # place, so there is no buffered tail left to flush here.


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
