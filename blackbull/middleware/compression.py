"""Negotiated body compression.

Optional codec installs change the available set. Preserve Vary so shared
caches cannot replay an encoded body to a client that refused its coding.
"""
import asyncio
import functools
import gzip
import threading
from collections.abc import Callable
from ..connection import Connection
from ..headers import _MinimalResponseHeaders
from ..native import NativeResponse
from ..protocol.framing import is_informational
from ..server.cap_log import log_cap_hit
from ._accept_encoding import select_encoding
from .utils import as_middleware

_MIN_SIZE = 100  # default minimum body size to bother compressing
_EXECUTOR_THRESHOLD = 65536  # default body size above which compression is offloaded
# Dynamic brotli quality; BB_BROTLI_QUALITY overrides it.
_BROTLI_QUALITY = 4
# Default cap on concurrent executor offloads.  When at the cap, additional
# eligible responses are served *uncompressed* rather than queued — bounded
# fall-back instead of unbounded executor queue growth.  ``0`` disables.
import os as _os  # noqa: PLC0415
from ..protocol.field_grammar import list_members, media_type
_MAX_INFLIGHT = max((_os.cpu_count() or 1) * 2, 4)

# Skip compressed/binary media. Do not skip font/ wholesale: TTF, OTF and
# SFNT still need compression, while WOFF and WOFF2 are already compressed.
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


def _is_compressible_content_type(fields: list[tuple[bytes, bytes]]) -> bool:
    """Return False when the Content-Type in response *fields* (lowercase
    names) signals already-compressed content."""
    ct = next((value for name, value in fields if name == b'content-type'), b'')
    ct_str = media_type(ct).decode('ascii', errors='ignore')
    return not any(ct_str.startswith(prefix) for prefix in _SKIP_CONTENT_TYPES)


def _merge_vary(headers: _MinimalResponseHeaders,
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
        if k == b'vary':
            tokens = list_members(v)
            if b'*' in tokens or field_l in tokens:
                return
            headers[i] = (k, v + b', ' + field)
            return
    headers.add(b'vary', field)


def _stamp_vary_if_compressible(header: _MinimalResponseHeaders) -> bool:
    """Whether *header* describes a body worth compressing; stamps ``Vary``.

    The decision point shared by every native exit: a compressible
    Content-Type that is not already encoded is a compression candidate, and
    its body varies by ``Accept-Encoding`` on *all* outcomes — compressed,
    too small, executor at cap, or handed to ``sendfile`` — so ``Vary`` is
    stamped here rather than only where compression succeeds.  Mutates
    *header* in place (zero-copy; the caller owns the list).
    """
    if not _is_compressible_content_type(header):
        return False
    if any(k == b'content-encoding' for k, _ in header):
        return False
    _merge_vary(header)
    return True


def _consume_outcome(future) -> None:
    """Read an offload's outcome, so an abandoned failure is not logged as an
    unretrieved exception."""
    if not future.cancelled():
        future.exception()


class _Permit:
    """One offload's permit, returned once.

    Two paths return it — the worker's ``finally``, and the submitting
    coroutine when the executor refuses the job — and only one of them can.
    """

    __slots__ = ('_middleware', '_returned')

    def __init__(self, middleware: 'Compression') -> None:
        self._middleware = middleware
        self._returned = False

    def release(self) -> None:
        with self._middleware._executor_lock:
            if not self._returned:
                self._returned = True
                self._middleware._executor_inflight -= 1


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
        # The offload's own thread returns the permit, so the counter is shared.
        self._executor_lock = threading.Lock()
        self._available = _detect_codecs(brotli_quality=brotli_quality)
        # Bound the selection cache: its header-byte keys are peer-controlled.
        self._codec_cache: dict[bytes, tuple[str, Callable[[bytes], bytes]] | None] = {}

    def _select_codec(self, accept_header: bytes) -> tuple[str, Callable[[bytes], bytes]] | None:
        """Select an installed accepted codec in server order: br, zstd, gzip.

        Ignore positive q-value ordering; q=0 forbids a codec. Return None on no overlap.
        """
        cache = self._codec_cache
        if accept_header in cache:
            return cache[accept_header]
        codec = select_encoding(accept_header, self._available)
        result = (codec, self._available[codec]) if codec is not None else None
        if len(cache) < 256:
            cache[accept_header] = result
        return result

    async def _compress(self, compressor: Callable[[bytes], bytes],
                        body: bytes) -> bytes | None:
        """Offload compression under the in-flight admission cap.

        Return None at the cap so callers serve uncompressed. This limits outstanding
        work, not duration; small bodies are compressed inline by the caller.
        """
        loop = asyncio.get_running_loop()
        # At the admission cap, serve uncompressed rather than queue more work.
        with self._executor_lock:
            inflight = self._executor_inflight
            capped = (self._executor_max_inflight > 0
                      and inflight >= self._executor_max_inflight)
            if not capped:
                self._executor_inflight = inflight + 1
        if capped:
            log_cap_hit('compression_max_inflight',
                        requested=inflight + 1,
                        limit=self._executor_max_inflight,
                        protocol='compression')
            return None

        permit = _Permit(self)

        def run() -> bytes:
            try:
                return compressor(body)
            finally:
                # The permit follows the work: a cancelled request leaves this
                # thread running.
                permit.release()

        try:
            future = loop.run_in_executor(None, run)
        except BaseException:
            permit.release()
            raise
        future.add_done_callback(_consume_outcome)
        # A cancelled request must not drop the submitted work: this thread is
        # the only thing that returns its permit.
        return await asyncio.shield(future)

    @staticmethod
    def _vary_ensuring_send(send):
        """Wrap *send* so a compressible, not-yet-encoded ``ResponseStart`` gains
        ``Vary: Accept-Encoding`` — used on the no-matching-codec path where the
        body is forwarded verbatim but must still be cache-keyed on the encoding
        Same predicate as the compress path's decision
        point: compressible Content-Type AND no pre-existing Content-Encoding.
        """
        # Unannotated on purpose: rebuilt per request (see _wrap_send_native in
        # app.py).  ``event`` is a NativeResponse or an ASGISendEvent.  The
        # import lives at per-request scope — inside the per-event closure it
        # would re-bind for every chunk of a streamed response.

        async def vary_send(event):
            # H1 native path: the header arm is a NativeResponse — stamp Vary
            # directly on its header list (zero-copy; no expansion).  Absence
            # is ``is not None`` — never truthiness.
            if (isinstance(event, NativeResponse)
                    and (event._extension is None or event.push is None)):
                if event._header is not None:
                    header = event._header
                    if _is_compressible_content_type(header) and not any(
                            k == b'content-encoding' for k, _ in header):
                        _merge_vary(event._header)
            await send(event)
        return vary_send

    async def __call__(self, conn, receive, send, call_next):
        # Native Connection for HTTP and WebSocket; the guard is defensive
        # against a raw ASGI scope dict (only reachable outside BlackBull's own
        # dispatch).
        if not isinstance(conn, Connection):
            await call_next(conn, receive, send)
            return

        accept = conn.headers.get_combined(b'accept-encoding') or b''
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

        # Unannotated on purpose: rebuilt per request (see _wrap_send_native in
        # app.py).  ``event`` is a NativeResponse or an ASGISendEvent.  The
        # import lives at per-request scope — inside the per-event closure it
        # would re-bind for every chunk of a streamed response.

        async def _emit_native_complete(status, header, body,
                                        original=None) -> None:
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
                    existing = header.copy()
                    existing.discard(b'content-length')
                    existing.add(b'content-encoding', codec_name.encode())
                    existing.add(b'content-length', str(len(compressed)).encode())
                    _merge_vary(existing)
                    await send(NativeResponse.complete(status, existing, compressed))
                    return
            # Uncompressed forward: pre-encoded / non-compressible / too-small
            # / executor-at-cap.  Vary is already stamped on *header* when this
            # response was a candidate, so either way the object carries the
            # correct cache key.
            await send(original if original is not None else NativeResponse(
                status=status, header=header, body=body))

        async def _release_pending(held) -> None:
            """Send held headers before a streaming body or pathsend; stop compressing.

            The sender requires a preceding start for pathsend.
            """
            nonlocal start_forwarded, skip_compression
            _stamp_vary_if_compressible(held._header)
            await send(held)
            start_forwarded = True
            skip_compression = True

        async def intercepting_send(event):
            nonlocal streaming, skip_compression, start_forwarded
            nonlocal pending_header
            # Keep complete and split head/body responses native; expanding them to
            # ASGI would duplicate boundary work. Trailers retain the event lane.
            if (isinstance(event, NativeResponse)
                    and (event._extension is None or event.push is None)):
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
                            and event._trailers is None):
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
                        and event._trailers is None):
                    await _emit_native_complete(
                        event.status, event._header, event._body,
                        original=event)
                    start_forwarded = True
                    return

                if is_informational(int(event.status)):
                    # An interim carries no content, so holding it for a
                    # body waits for one that cannot come.
                    await send(event)
                    return

                if (not streaming and not skip_compression
                        and not start_forwarded
                        and event._header is not None
                        and event._body is None
                        and not event.expects_trailers
                        and event._trailers is None):
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

            # A push message or an event the native seam does not
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
