"""ASGI bridge that serves gRPC calls over BlackBull's HTTP/2 layer.

gRPC is HTTP/2 with a fixed request shape (``POST /package.Service/Method``,
``content-type: application/grpc``) and a Length-Prefixed-Message body, where
the call result is reported in ``grpc-status`` / ``grpc-message`` *trailers*.
BlackBull's HTTP/2 sender already emits trailers via the
``http.response.trailers`` ASGI event, and ``HTTP2Recipient`` already delivers
request DATA as incremental ``http.request`` events, so all four RPC kinds —
unary, server-, client-, and bidirectional-streaming — map cleanly onto the
existing (scope, receive, send) bridge; no new protocol Actor is needed
(see ``.claude/planning/designs/grpc-streaming-request-design.md``).

``serve_grpc`` is dispatched from :meth:`BlackBull._dispatch` when the request
content-type is ``application/grpc`` and a registry was installed via
``app.enable_grpc(...)``.
"""
from __future__ import annotations

import asyncio
import logging
import os
import struct

from ..request import read_body, ClientDisconnected
from . import compression
from .codec import decode_messages, encode_message, GrpcDecodeError, MAX_MESSAGE_LENGTH
from .registry import GrpcServiceRegistry
from .status import GrpcError, GrpcStatus

logger = logging.getLogger(__name__)

_GRPC_CONTENT_TYPE = b'application/grpc'

# Advertised in ``grpc-accept-encoding`` so clients know which message
# encodings the server can decode (``identity`` + ``gzip``).
_GRPC_ACCEPT_ENCODING = compression.ACCEPT_ENCODING

# Per-message size cap at the gRPC layer (RESOURCE_EXHAUSTED above it).  4 MiB
# matches grpcio's default receive limit; override with BB_GRPC_MAX_MESSAGE_SIZE.
# Read at import; tests may monkeypatch this module attribute.
try:
    MAX_MESSAGE_SIZE = int(os.environ.get('BB_GRPC_MAX_MESSAGE_SIZE', 4 * 1024 * 1024))
except ValueError:
    MAX_MESSAGE_SIZE = 4 * 1024 * 1024

# Server-streaming write-coalescing threshold.  Each yielded message otherwise
# becomes its own ``http.response.body`` event → its own DATA frame → its own
# drain; a handler streaming thousands of small messages then pays thousands of
# per-message round-trips through the sender + event loop, and under many
# concurrent streams that dominates (a 5000-message call took 0.1–2.3 s, so a
# real gRPC client with a deadline times the call out / closes the connection
# mid-stream — the "streaming collapse" the bench hit).  Coalescing consecutive
# messages into one DATA frame (up to ~one default max-frame worth) cuts the
# round-trips ~1000× for bulk streams.  Multiple length-prefixed gRPC messages
# in one DATA frame is valid on the wire (clients frame on the 5-byte prefix,
# not on DATA boundaries).  The buffer is always flushed at stream end and
# before any trailing status, so no message is ever withheld past the call.
try:
    _STREAM_BATCH_BYTES = int(os.environ.get('BB_GRPC_STREAM_BATCH_BYTES', 16 * 1024))
except ValueError:
    _STREAM_BATCH_BYTES = 16 * 1024

# Response messages larger than this are gzip-compressed when the client's
# grpc-accept-encoding lists gzip (and compression actually shrinks them).
# Small messages compress poorly — the gzip header/trailer overhead can make
# them *larger* — so a threshold avoids burning CPU for no bandwidth win.  Set
# BB_GRPC_COMPRESS_MIN_BYTES very high to effectively disable response
# compression.  Read at import; tests may monkeypatch this module attribute.
try:
    _COMPRESS_MIN_BYTES = int(os.environ.get('BB_GRPC_COMPRESS_MIN_BYTES', 1024))
except ValueError:
    _COMPRESS_MIN_BYTES = 1024

# If pulling the next message took longer than this, the producer awaited
# between messages (a slow/interactive stream) rather than yielding a
# synchronous burst — flush the partial batch now so its output isn't withheld
# until the batch fills.  Comfortably above a synchronous __anext__'s cost
# (sub-microsecond) and well below any human-perceptible streaming interval.
_STREAM_FLUSH_IDLE_S = 0.0005

# grpc-timeout unit → seconds (gRPC HTTP/2 protocol §"Timeout").
_TIMEOUT_UNITS: dict[bytes, float] = {
    b'H': 3600.0, b'M': 60.0, b'S': 1.0,
    b'm': 0.001, b'u': 0.000001, b'n': 1.0e-9,
}


def _parse_grpc_timeout(raw: bytes) -> float | None:
    """Parse a ``grpc-timeout`` header (``<1-8 digits><H|M|S|m|u|n>``) into
    seconds, or ``None`` for absent/invalid values (the spec says an
    unparseable timeout SHOULD be ignored)."""
    if not raw:
        return None
    mult = _TIMEOUT_UNITS.get(raw[-1:])
    if mult is None:
        return None
    digits = raw[:-1]
    if not digits or len(digits) > 8 or not digits.isdigit():
        return None
    value = int(digits)
    if value <= 0:
        return None
    return value * mult


def _pct_encode_message(details: str) -> bytes:
    """Percent-encode a ``grpc-message`` value per the gRPC HTTP/2 spec.

    ASCII 0x20–0x7E except ``%`` pass through; everything else (including
    non-ASCII, encoded UTF-8 first) becomes ``%XX``.
    """
    out = bytearray()
    for b in details.encode('utf-8'):
        if 0x20 <= b <= 0x7E and b != 0x25:  # printable ASCII, not '%'
            out.append(b)
        else:
            out += b'%%%02X' % b
    return bytes(out)


def _accepts_gzip(accept: bytes) -> bool:
    """Return ``True`` if the client's ``grpc-accept-encoding`` lists ``gzip``.

    The header is a comma-separated list of message encodings the client can
    decode (e.g. ``identity,deflate,gzip``); the server may compress responses
    with any it recognises."""
    return any(tok.strip().lower() == b'gzip'
               for tok in (accept or b'').split(b','))


def _decompress_message(message: bytes, encoding: bytes) -> bytes:
    """Decompress a request message whose LPM Compressed-Flag is set, using the
    request's ``grpc-encoding``.

    Raises :class:`GrpcError`: UNIMPLEMENTED for an unsupported / absent
    encoding (the server's ``grpc-accept-encoding`` is advertised on the
    response so the client can retry uncompressed), RESOURCE_EXHAUSTED for a
    decompression bomb, INTERNAL for a corrupt stream."""
    if encoding == b'gzip':
        try:
            return compression.decompress_gzip(message, MAX_MESSAGE_SIZE)
        except compression.DecompressionBombError as exc:
            raise GrpcError(GrpcStatus.RESOURCE_EXHAUSTED, str(exc))
        except compression.DecompressionError as exc:
            raise GrpcError(GrpcStatus.INTERNAL, f'malformed request: {exc}')
    name = encoding.decode('ascii', 'replace') or 'identity'
    raise GrpcError(
        GrpcStatus.UNIMPLEMENTED,
        f'grpc-encoding {name!r} is not supported for compressed messages; '
        f'server accepts {_GRPC_ACCEPT_ENCODING.decode()}')


def _frame_response(payload: bytes, compress: bool) -> bytes:
    """Frame *payload* as a Length-Prefixed-Message, gzip-compressing it
    (Compressed-Flag = 1) when *compress* is set, the message is over
    ``_COMPRESS_MIN_BYTES``, and compression actually shrinks it.

    A per-message opt-out (sending an over-threshold-but-incompressible or a
    small message uncompressed with Flag = 0) is valid even when the response's
    ``grpc-encoding`` header advertises gzip — the flag, not the header, decides
    each message."""
    if compress and len(payload) > _COMPRESS_MIN_BYTES:
        packed = compression.compress_gzip(payload)
        if len(packed) < len(payload):
            return encode_message(packed, compressed=True)
    return encode_message(payload)


class GrpcContext:
    """Per-call context handed to a gRPC handler.

    Exposes request metadata (the HTTP/2 headers), the call deadline and peer,
    and lets the handler set the outgoing status, a human-readable message,
    leading/trailing metadata, or abort the call outright — the subset of
    grpcio's ``ServicerContext`` that a raw-bytes transport can honour.

    The response-start machinery (``_send`` … ``_started``) is bound by
    :func:`serve_grpc` just before the handler runs; handlers touch it only via
    :meth:`send_initial_metadata`.
    """

    __slots__ = ('scope', 'code', 'details', '_trailing', '_deadline',
                 '_send', '_content_type', '_response_encoding',
                 '_initial_metadata', '_started')

    def __init__(self, scope: dict):
        self.scope = scope
        self.code: GrpcStatus = GrpcStatus.OK
        self.details: str = ''
        self._trailing: list[tuple[bytes, bytes]] = []
        # Bound by _bind() at dispatch; defaults keep a hand-built context
        # (tests) usable without binding.
        self._deadline: float | None = None          # absolute loop time, or None
        self._send = None
        self._content_type: bytes = _GRPC_CONTENT_TYPE
        self._response_encoding: bytes | None = None
        self._initial_metadata: list[tuple[bytes, bytes]] = []
        self._started: bool = False

    def _bind(self, send, content_type: bytes, response_encoding: bytes | None,
              deadline: float | None) -> None:
        """Wire the response side (called by :func:`serve_grpc`).  *deadline* is
        a duration in seconds; it is stored as an absolute loop time so
        :meth:`time_remaining` counts down from here."""
        self._send = send
        self._content_type = content_type
        self._response_encoding = response_encoding
        if deadline is not None:
            self._deadline = asyncio.get_event_loop().time() + deadline

    def metadata(self, name: bytes, default: bytes = b'') -> bytes:
        """Return a request header (call metadata) value, or *default*."""
        headers = self.scope.get('headers')
        getter = getattr(headers, 'get', None)
        if getter is not None and not isinstance(headers, (list, tuple)):
            return getter(name, default)
        for k, v in headers or ():
            if k.lower() == name.lower():
                return v
        return default

    def invocation_metadata(self) -> list[tuple[bytes, bytes]]:
        """Return all request metadata (HTTP/2 headers) as ``(name, value)``
        pairs — grpcio's ``ServicerContext.invocation_metadata``.  Pseudo-
        headers (``:method``, ``:path``, …) are excluded; they are call routing,
        not application metadata."""
        headers = self.scope.get('headers') or ()
        return [(k, v) for k, v in headers if not k.startswith(b':')]

    def peer(self) -> str:
        """Return the client address as grpcio formats it (``ipv4:host:port`` /
        ``ipv6:[host]:port``), or ``''`` when the transport did not supply one."""
        client = self.scope.get('client')
        if not client:
            return ''
        host, port = client[0], client[1]
        if ':' in str(host):        # IPv6 literal
            return f'ipv6:[{host}]:{port}'
        return f'ipv4:{host}:{port}'

    def time_remaining(self) -> float | None:
        """Seconds left until the call deadline (never negative), or ``None``
        when the client set no ``grpc-timeout`` — grpcio's
        ``ServicerContext.time_remaining``.  Lets a handler shed work it cannot
        finish in time."""
        if self._deadline is None:
            return None
        return max(0.0, self._deadline - asyncio.get_event_loop().time())

    def set_code(self, status: GrpcStatus) -> None:
        # Enum-only, matching grpcio's ServicerContext.set_code (see
        # GrpcError.__init__ for why a raw int is never a valid input here).
        self.code = GrpcStatus(status)

    def set_details(self, details: str) -> None:
        self.details = details

    def set_trailing_metadata(self, metadata) -> None:
        self._trailing = [(k, v) for k, v in metadata]

    async def send_initial_metadata(self, metadata) -> None:
        """Send response leading metadata now (the initial HTTP/2 HEADERS),
        before the first response message — grpcio's
        ``ServicerContext.send_initial_metadata``.

        Optional: if never called, the response HEADERS are still emitted lazily
        (just before the first message, or with the trailers for an empty
        response).  Calling it flushes them early with *metadata* attached — used
        to hand the client leading metadata (auth challenges, stream ids) up
        front.  Raises :class:`ValueError` once the HEADERS have already gone
        out (grpcio's "initial metadata no longer allowed")."""
        if self._started:
            raise ValueError('initial metadata already sent')
        self._initial_metadata = [(k, v) for k, v in metadata]
        await self._start_response()

    async def _start_response(self) -> None:
        """Emit the response HEADERS exactly once (idempotent).  All response
        writers funnel through here so :meth:`send_initial_metadata` and the
        lazy first-message path can't double-send the start event."""
        if self._started:
            return
        self._started = True
        await self._send(_response_start(
            self._content_type, self._response_encoding, self._initial_metadata))

    def abort(self, status: GrpcStatus, details: str = '') -> None:
        """Raise :class:`GrpcError` to end the call with a non-OK status."""
        raise GrpcError(status, details)


def _resolve_content_type(raw: bytes) -> bytes:
    """Return the response content-type, echoing a valid ``application/grpc``
    request subtype (e.g. ``application/grpc+proto``) and tolerating
    surrounding whitespace; falls back to bare ``application/grpc``."""
    ct = (raw or b'').strip()
    if ct == _GRPC_CONTENT_TYPE or ct.startswith(_GRPC_CONTENT_TYPE + b'+'):
        return ct
    return _GRPC_CONTENT_TYPE


def _status_trailers(status: GrpcStatus, details: str,
                     extra: list[tuple[bytes, bytes]] | None = None
                     ) -> list[tuple[bytes, bytes]]:
    trailers = [(b'grpc-status', str(int(status)).encode('ascii'))]
    if details:
        trailers.append((b'grpc-message', _pct_encode_message(details)))
    if extra:
        trailers.extend(extra)
    return trailers


async def _send_trailers_only(send, status: GrpcStatus, details: str,
                              content_type: bytes = _GRPC_CONTENT_TYPE) -> None:
    """Emit a gRPC error response: HTTP 200, no message, ``grpc-status`` in
    a *trailing* HEADERS frame.

    A strict gRPC client (grpcio, grpc-go) reads ``grpc-status`` only from a
    HEADERS frame carrying END_STREAM (true Trailers-Only) or from a trailing
    HEADERS frame — never from a non-terminal HEADERS frame.  The earlier
    implementation put ``grpc-status`` in the initial HEADERS and then sent an
    empty END_STREAM DATA frame; the client read the HEADERS as ordinary
    initial metadata, found no trailing status, and surfaced UNKNOWN
    ("Stream removed (Data frame with END_STREAM flag received)").  BlackBull's
    own ``HTTP2Client`` is lenient about this, which hid the bug.

    Routing through ``http.response.start(trailers=True)`` + ``…trailers`` emits
    Response-Headers followed by a trailing HEADERS frame (END_STREAM) with the
    status — the same trailers machinery the success path uses, minus the DATA
    frame — which every conformant gRPC client accepts."""
    await send({'type': 'http.response.start', 'status': 200,
                'headers': [(b'content-type', content_type),
                            (b'grpc-accept-encoding', _GRPC_ACCEPT_ENCODING)],
                'trailers': True})
    await send({'type': 'http.response.trailers',
                'headers': _status_trailers(status, details)})


async def _read_unary_request(receive, encoding: bytes) -> bytes:
    """Read the request body and return the single de-framed request message.

    Server-streaming still takes exactly one request message (only the response
    streams), so unary and server-streaming share this.  A compressed message
    (Compressed-Flag = 1) is decompressed with the request's *encoding* (the
    ``grpc-encoding`` header); the per-message size limit applies to the
    decompressed output.  Raises :class:`GrpcError` on a malformed /
    multi-message / unsupported-encoding / oversized request."""
    try:
        body = await read_body(receive)
    except ClientDisconnected as exc:
        # RST_STREAM / client disconnect before the request finished — the
        # canonical gRPC mapping is CANCELLED, not a fabricated INTERNAL over
        # a truncated body (bug 1.11).
        raise GrpcError(
            GrpcStatus.CANCELLED,
            'client disconnected before sending the request') from exc
    try:
        messages = decode_messages(body)
    except GrpcDecodeError as exc:
        raise GrpcError(GrpcStatus.INTERNAL, f'malformed request: {exc}')
    if len(messages) != 1:
        raise GrpcError(
            GrpcStatus.UNIMPLEMENTED,
            f'method expects exactly 1 request message, got {len(messages)}')
    compressed, request = messages[0]
    if compressed:
        request = _decompress_message(request, encoding)
    if len(request) > MAX_MESSAGE_SIZE:
        raise GrpcError(
            GrpcStatus.RESOURCE_EXHAUSTED,
            f'request message ({len(request)} bytes) larger than the '
            f'{MAX_MESSAGE_SIZE}-byte limit')
    return request


# 1-byte compressed flag + 4-byte big-endian length (gRPC LPM prefix).
_LPM_PREFIX = struct.Struct('>BI')
_PREFIX_LEN = _LPM_PREFIX.size


async def _iter_request_messages(receive, encoding: bytes):
    """Yield de-framed request messages as they arrive (client-/bidi-streaming).

    Reassembles Length-Prefixed-Messages across ``http.request`` events — gRPC
    messages don't align to DATA-frame boundaries, so a message may straddle
    several events or several messages may share one.  A residual buffer holds
    the partial tail between events.  A compressed message (Compressed-Flag = 1)
    is decompressed with the request's *encoding*.  Raises :class:`GrpcError` on
    an oversized / unsupported-encoding / truncated message, matching
    ``_read_unary_request`` (RESOURCE_EXHAUSTED / UNIMPLEMENTED / INTERNAL)."""
    buf = bytearray()
    more = True
    while more:
        event = await receive()
        if event.get('type') == 'http.disconnect':
            raise GrpcError(GrpcStatus.CANCELLED, 'client disconnected mid-stream')
        chunk = event.get('body', b'')
        if chunk:
            buf.extend(chunk)
        more = event.get('more_body', False)
        # Drain every complete message currently buffered.
        while len(buf) >= _PREFIX_LEN:
            flag, length = _LPM_PREFIX.unpack_from(buf, 0)
            # For an uncompressed frame the prefixed length *is* the message
            # size, so the per-message limit applies directly.  A compressed
            # frame's length is the *compressed* transfer size (which may
            # inflate); it is bounded only by the codec safety floor here, and
            # the real per-message limit is enforced on the decompressed output
            # by _decompress_message.
            limit = MAX_MESSAGE_LENGTH if flag else MAX_MESSAGE_SIZE
            if length > limit:
                raise GrpcError(
                    GrpcStatus.RESOURCE_EXHAUSTED,
                    f'request message ({length} bytes) larger than the '
                    f'{limit}-byte limit')
            if len(buf) - _PREFIX_LEN < length:
                break  # body not fully arrived yet
            message = bytes(buf[_PREFIX_LEN:_PREFIX_LEN + length])
            del buf[:_PREFIX_LEN + length]
            if flag:
                message = _decompress_message(message, encoding)
            yield message
    if buf:
        raise GrpcError(
            GrpcStatus.INTERNAL,
            f'malformed request: {len(buf)} trailing byte(s) after last message')


def _validate_response_message(response) -> bytes:
    """Return *response* as ``bytes`` or raise :class:`GrpcError` (INTERNAL for
    a wrong type, RESOURCE_EXHAUSTED when it exceeds the per-message limit)."""
    if not isinstance(response, (bytes, bytearray)):
        raise GrpcError(
            GrpcStatus.INTERNAL,
            f'handler returned {type(response).__name__}, expected bytes')
    if len(response) > MAX_MESSAGE_SIZE:
        raise GrpcError(
            GrpcStatus.RESOURCE_EXHAUSTED,
            f'response message ({len(response)} bytes) larger than the '
            f'{MAX_MESSAGE_SIZE}-byte limit')
    return bytes(response)


def _response_start(content_type: bytes,
                    response_encoding: bytes | None = None,
                    initial_metadata: list[tuple[bytes, bytes]] | None = None
                    ) -> dict:
    headers = [(b'content-type', content_type),
               (b'grpc-accept-encoding', _GRPC_ACCEPT_ENCODING)]
    # Advertise the encoding used for any compressed response messages.  Present
    # whenever gzip was negotiated, even if a particular message rides
    # uncompressed (Flag = 0) — this mirrors grpcio, and the flag decides each
    # message regardless.
    if response_encoding:
        headers.append((b'grpc-encoding', response_encoding))
    # Handler-supplied leading metadata (context.send_initial_metadata).
    if initial_metadata:
        headers.extend(initial_metadata)
    return {'type': 'http.response.start', 'status': 200,
            'headers': headers, 'trailers': True}


async def _serve_unary(handler, request, context, send, content_type,
                       deadline: float | None,
                       response_encoding: bytes | None = None) -> None:
    """Run a unary handler and emit HEADERS → one DATA → status trailers.

    Normally nothing is written until the response is computed, so a failure is
    a clean Trailers-Only error.  A handler that called
    ``context.send_initial_metadata`` has already flushed HEADERS, so an error
    after that rides the trailing HEADERS frame instead — ``_finish_stream_error``
    picks the right shape from ``context._started``."""
    try:
        if deadline is not None:
            response = await asyncio.wait_for(
                handler(request, context), timeout=deadline)
        else:
            response = await handler(request, context)
        response = _validate_response_message(response)
    except GrpcError as exc:
        await _finish_stream_error(
            send, context._started, exc.status, exc.details, content_type)
        return
    except asyncio.TimeoutError:
        await _finish_stream_error(
            send, context._started, GrpcStatus.DEADLINE_EXCEEDED,
            'deadline exceeded', content_type)
        return
    except Exception as exc:  # noqa: BLE001 — handler isolation
        # Isolate handler bugs as INTERNAL.  CancelledError / KeyboardInterrupt /
        # SystemExit / GeneratorExit derive from BaseException (not Exception),
        # so they propagate here rather than being masked — task cancellation and
        # interpreter shutdown must never be turned into a gRPC status.
        logger.exception('gRPC unary handler raised')
        await _finish_stream_error(
            send, context._started, GrpcStatus.INTERNAL, str(exc), content_type)
        return

    await context._start_response()
    await send({'type': 'http.response.body',
                'body': _frame_response(response, response_encoding is not None),
                'more_body': True})
    await send({'type': 'http.response.trailers',
                'headers': _status_trailers(context.code, context.details,
                                            context._trailing)})


async def _serve_server_streaming(handler, request, context, send, content_type,
                                  deadline: float | None,
                                  response_encoding: bytes | None = None) -> None:
    """Drive a server-streaming (async-generator) handler.

    Response-Headers are sent lazily, just before the first message, so a
    handler that errors *before* yielding anything still produces a clean
    Trailers-Only error (like a unary failure).  Once a message has gone out the
    status can only ride the trailing HEADERS frame, so a mid-stream error is
    reported there.  The generator is always finalised (``aclose``) — including
    on client cancellation — so its ``finally``/cleanup runs."""
    agen = handler(request, context)
    # HEADERS are sent once via context._start_response() — idempotent, and
    # shared with context.send_initial_metadata so a handler that flushed
    # leading metadata early doesn't double-send the start event.
    # Coalesce consecutive messages into one DATA frame per flush.  Bytes are
    # accumulated as already-gRPC-framed messages (5-byte prefix + body) so the
    # buffer can be sent verbatim; flushed when it reaches _STREAM_BATCH_BYTES
    # and, unconditionally, at stream end / before any trailing status.
    buf = bytearray()

    async def _flush() -> None:
        if not buf:
            return
        await context._start_response()
        await send({'type': 'http.response.body',
                    'body': bytes(buf), 'more_body': True})
        buf.clear()

    loop = asyncio.get_event_loop()

    async def _drive() -> None:
        # Coalesce consecutive messages into one DATA frame.  A *synchronous*
        # burst (the bulk case — thousands of tiny messages yielded without
        # awaiting) buffers into ``buf`` and flushes only at ~one max-frame
        # worth, turning thousands of per-message sends+drains into a handful.
        # A producer that *awaits* between messages is detected by timing the
        # pull: when it exceeds _STREAM_FLUSH_IDLE_S the partial batch is flushed
        # so a slow/interactive stream isn't withheld.  The tail is flushed at
        # stream end / before any trailing status.
        it = agen.__aiter__()
        while True:
            t0 = loop.time()
            try:
                msg = await it.__anext__()
            except StopAsyncIteration:
                return
            paused = (loop.time() - t0) > _STREAM_FLUSH_IDLE_S
            buf.extend(_frame_response(_validate_response_message(msg),
                                       response_encoding is not None))
            if len(buf) >= _STREAM_BATCH_BYTES or (paused and buf):
                await _flush()

    try:
        if deadline is not None:
            async with asyncio.timeout(deadline):
                await _drive()
        else:
            await _drive()
    except GrpcError as exc:
        # Deliver messages already committed by the generator, then the status.
        await _flush()
        await _finish_stream_error(
            send, context._started, exc.status, exc.details, content_type)
        return
    except (asyncio.TimeoutError, TimeoutError):
        await _flush()
        await _finish_stream_error(
            send, context._started, GrpcStatus.DEADLINE_EXCEEDED,
            'deadline exceeded', content_type)
        return
    except Exception as exc:  # noqa: BLE001 — handler isolation
        # Isolate handler bugs as INTERNAL.  CancelledError / KeyboardInterrupt /
        # SystemExit / GeneratorExit derive from BaseException (not Exception),
        # so they propagate here — client cancellation still unwinds the stream
        # (and finalises the generator via the finally below) instead of being
        # reported as a gRPC status.  Messages the generator successfully yielded
        # before the crash are committed and delivered (they'd already be on the
        # wire without batching); only the failed message is lost.
        await _flush()
        logger.exception('gRPC server-streaming handler raised')
        await _finish_stream_error(
            send, context._started, GrpcStatus.INTERNAL, str(exc), content_type)
        return
    finally:
        # Finalise the generator so its cleanup runs on every exit path
        # (normal completion, error, or client cancellation).  Best-effort:
        # a raising aclose() must not mask the status we already reported.
        try:
            await agen.aclose()
        except Exception:  # noqa: BLE001
            logger.exception('gRPC server-streaming generator aclose() raised')

    # Success: flush the final partial batch, then the OK trailer.  For an empty
    # stream (nothing buffered / yielded) _flush is a no-op and the headers are
    # sent here so the client still sees Response-Headers + Trailers.
    await _flush()
    await context._start_response()  # idempotent — emits HEADERS if not yet sent
    await send({'type': 'http.response.trailers',
                'headers': _status_trailers(context.code, context.details,
                                            context._trailing)})


async def _finish_stream_error(send, started: bool, status: GrpcStatus,
                               details: str, content_type: bytes) -> None:
    """Report a streaming error: in trailers if Response-Headers already went
    out, otherwise as a Trailers-Only response."""
    if started:
        await send({'type': 'http.response.trailers',
                    'headers': _status_trailers(status, details)})
    else:
        await _send_trailers_only(send, status, details, content_type)


async def serve_grpc(registry: GrpcServiceRegistry, scope, receive, send) -> None:
    """Serve a single gRPC call (unary or server-streaming) through the ASGI
    (scope, receive, send) bridge.  Never raises for handler/protocol errors:
    every failure path is reported as a gRPC status."""
    path = scope.get('path', '')
    context = GrpcContext(scope)
    # Echo the request's content-type subtype (application/grpc+proto, +json, …)
    # back on the response, defaulting to bare application/grpc.
    content_type = _resolve_content_type(context.metadata(b'content-type'))

    method = registry.lookup_method(path)
    if method is None:
        await _send_trailers_only(
            send, GrpcStatus.UNIMPLEMENTED, f'Method not found: {path}', content_type)
        return

    # RFC: enforce the client's deadline (grpc-timeout) if it sent one.
    deadline = _parse_grpc_timeout(context.metadata(b'grpc-timeout'))

    # Compression negotiation.  The request's ``grpc-encoding`` names the coding
    # of its compressed messages; the client's ``grpc-accept-encoding`` says
    # what it can decode, so we may gzip responses only when it lists gzip.
    request_encoding = context.metadata(b'grpc-encoding').strip().lower()
    response_encoding = (
        b'gzip' if _accepts_gzip(context.metadata(b'grpc-accept-encoding'))
        else None)

    # Wire the context's response side now, so the handler can call
    # context.send_initial_metadata / time_remaining before any bytes go out.
    context._bind(send, content_type, response_encoding, deadline)

    # Request axis.  Request-unary reads + de-frames the whole body up front, so
    # a framing error precedes any response bytes (clean Trailers-Only).
    # Request-streaming hands the handler a lazy async iterator; its framing
    # errors surface while the handler runs and are reported by the serve
    # helpers (in trailers if a message already went out, else Trailers-Only).
    if method.client_streaming:
        request = _iter_request_messages(receive, request_encoding)
    else:
        try:
            request = await _read_unary_request(receive, request_encoding)
        except GrpcError as exc:
            await _send_trailers_only(send, exc.status, exc.details, content_type)
            return

    # Response axis reuses the same writers for both request kinds: passing the
    # request iterator where a unary handler takes ``request`` just calls
    # ``handler(request_iter, context)`` — client-streaming rides _serve_unary,
    # bidirectional rides _serve_server_streaming.
    try:
        if method.streaming:
            await _serve_server_streaming(
                method.handler, request, context, send, content_type, deadline,
                response_encoding)
        else:
            await _serve_unary(
                method.handler, request, context, send, content_type, deadline,
                response_encoding)
    finally:
        # Finalise the request generator so its cleanup runs even when the
        # handler returned without draining it (client-/bidi-streaming only).
        if method.client_streaming:
            await request.aclose()
