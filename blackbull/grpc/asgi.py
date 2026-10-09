"""gRPC call semantics over the existing HTTP/2 receive/send bridge.

Status is reported in trailers; reuse stream isolation and flow control
rather than introducing another protocol actor.
"""
from __future__ import annotations

import asyncio
import base64
import binascii
import logging
import os

from ..native import NativeResponse
from ..request import stream_body, ClientDisconnected
from ..utils import create_eager_task
from . import compression
from .codec import encode_message, MAX_MESSAGE_LENGTH, _PREFIX, _PREFIX_LEN
from .registry import GrpcServiceRegistry
from .status import GrpcError, GrpcStatus

logger = logging.getLogger(__name__)

_TERMINATION_TIMEOUT = 0.1


class _RpcDeadlineExceeded(TimeoutError):
    pass


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

# Batch length-prefixed messages independently of DATA boundaries.
# Flush partial batches on producer suspension, at end, and before status.
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

    Raises [`GrpcError`][]: UNIMPLEMENTED for an unsupported / absent
    encoding (the server's ``grpc-accept-encoding`` is advertised on the
    response so the client can retry uncompressed), RESOURCE_EXHAUSTED for a
    decompression bomb, INTERNAL for a corrupt or incomplete stream."""
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


def _req_field(conn, name, default=None):
    """Read a request field from either a native
    [`Connection`][blackbull.connection.Connection] (the ``serve_grpc(conn, …)`` path)
    or an ASGI ``scope`` dict — the field names (``headers``/``client``/``path``)
    coincide with the Connection attributes."""
    if isinstance(conn, dict):
        return conn.get(name, default)
    return getattr(conn, name, default)


class GrpcContext:
    """Per-call metadata, peer, deadline and response status/metadata controls for raw-byte handlers.
    """

    __slots__ = ('conn', 'code', 'details', '_trailing', '_deadline',
                 '_send', '_content_type', '_response_encoding',
                 '_initial_metadata', '_started', '_deadline_expired', '_clock')

    def __init__(self, conn):
        self.conn = conn
        self.code: GrpcStatus = GrpcStatus.OK
        self.details: str = ''
        self._trailing: list[tuple[bytes, bytes]] = []
        # Bound by _bind() at dispatch; defaults keep a hand-built context
        # (tests) usable without binding.
        self._deadline: float | None = None          # absolute loop time, or None
        self._deadline_expired = False
        self._clock = None
        self._send = None
        self._content_type: bytes = _GRPC_CONTENT_TYPE
        self._response_encoding: bytes | None = None
        self._initial_metadata: list[tuple[bytes, bytes]] = []
        self._started: bool = False

    def _bind(self, send, content_type: bytes, response_encoding: bytes | None,
              deadline: float | None) -> None:
        """Bind the response side and absolute loop-time deadline."""
        self._send = send
        self._content_type = content_type
        self._response_encoding = response_encoding
        self._deadline = deadline
        self._deadline_expired = False
        self._clock = asyncio.get_running_loop().time if deadline is not None else None

    def metadata(self, name: bytes, default: bytes = b'') -> bytes:
        """Return a request header (call metadata) value, or *default*."""
        headers = _req_field(self.conn, 'headers')
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
        headers = _req_field(self.conn, 'headers') or ()
        return [(k, v) for k, v in headers if not k.startswith(b':')]

    def peer(self) -> str:
        """Return the client address as grpcio formats it (``ipv4:host:port`` /
        ``ipv6:[host]:port``), or ``''`` when the transport did not supply one."""
        client = _req_field(self.conn, 'client')
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
        if self._deadline_expired:
            return 0.0
        return max(0.0, self._deadline - self._clock())

    def set_code(self, status: GrpcStatus) -> None:
        # Enum-only, matching grpcio's ServicerContext.set_code (see
        # GrpcError.__init__ for why a raw int is never a valid input here).
        self.code = GrpcStatus(status)

    def set_details(self, details: str) -> None:
        self.details = details

    def set_trailing_metadata(self, metadata) -> None:
        self._trailing = [(k, v) for k, v in metadata]

    def trailing_metadata(self) -> list[tuple[bytes, bytes]]:
        """The trailing metadata set so far, as a copy — so a helper composes
        with what the handler already set instead of clobbering it."""
        return list(self._trailing)

    async def send_initial_metadata(self, metadata) -> None:
        """Send response leading metadata now (the initial HTTP/2 HEADERS),
        before the first response message — grpcio's
        ``ServicerContext.send_initial_metadata``.

        Optional: if never called, the response HEADERS are still emitted lazily
        (just before the first message, or with the trailers for an empty
        response).  Calling it flushes them early with *metadata* attached — used
        to hand the client leading metadata (auth challenges, stream ids) up
        front.  Raises ``ValueError`` once the HEADERS have already gone
        out (grpcio's "initial metadata no longer allowed")."""
        if self._started:
            raise ValueError('initial metadata already sent')
        self._initial_metadata = [(k, v) for k, v in metadata]
        await self._start_response()

    async def _start_response(self) -> None:
        """Emit the response HEADERS exactly once (idempotent).  All response
        writers funnel through here so [`send_initial_metadata`][] and the
        lazy first-message path can't double-send the start event."""
        if self._started:
            return
        if self._deadline is not None:
            _check_deadline(self)
        self._started = True
        await self._send(_response_start(
            self._content_type, self._response_encoding, self._initial_metadata))

    def abort(self, status: GrpcStatus, details: str = '') -> None:
        """Raise [`GrpcError`][] to end the call with a non-OK status."""
        raise GrpcError(status, details)


def _resolve_content_type(raw: bytes) -> bytes:
    """Return the response content-type, echoing a valid ``application/grpc``
    request subtype (e.g. ``application/grpc+proto``) and tolerating
    surrounding whitespace; falls back to bare ``application/grpc``."""
    ct = (raw or b'').strip()
    if ct == _GRPC_CONTENT_TYPE or ct.startswith(_GRPC_CONTENT_TYPE + b'+'):
        return ct
    return _GRPC_CONTENT_TYPE


def _normalized_base64(value: bytes) -> bytes | None:
    """Return canonical unpadded base64 when *value* already has that form."""
    if value != value.rstrip(b'='):
        return None
    unpadded = value.rstrip(b'=')
    try:
        decoded = base64.b64decode(
            unpadded + b'=' * (-len(unpadded) % 4), validate=True)
    except (binascii.Error, ValueError, TypeError):
        return None
    normalized = base64.b64encode(decoded).rstrip(b'=')
    return normalized if normalized == unpadded else None


def _canonical_varint(data: bytes, position: int) -> tuple[int, int] | None:
    value = 0
    start = position
    for shift in range(0, 70, 7):
        if position == len(data):
            return None
        octet = data[position]
        position += 1
        if shift == 63 and octet > 1:
            return None
        value |= (octet & 0x7f) << shift
        if not octet & 0x80:
            width = max(1, (value.bit_length() + 6) // 7)
            return (value, position) if position - start == width else None
    return None


def _length_delimited(
        data: bytes, position: int) -> tuple[bytes, int] | None:
    length_at = _canonical_varint(data, position)
    if length_at is None:
        return None
    length, position = length_at
    end = position + length
    if end > len(data):
        return None
    return data[position:end], end


def _valid_utf8(value: bytes) -> bool:
    try:
        value.decode('utf-8')
    except UnicodeDecodeError:
        return False
    return True


def _canonical_any(data: bytes) -> bool:
    position = 0
    if position < len(data) and data[position] == 0x0a:
        type_url_at = _length_delimited(data, position + 1)
        if type_url_at is None:
            return False
        type_url, position = type_url_at
        if not type_url or not _valid_utf8(type_url):
            return False
    if position < len(data) and data[position] == 0x12:
        value_at = _length_delimited(data, position + 1)
        if value_at is None:
            return False
        value, position = value_at
        if not value:
            return False
    return position == len(data)


def _canonical_non_ok_status(data: bytes) -> bool:
    if not data or data[0] != 0x08:
        return False
    code_at = _canonical_varint(data, 1)
    if code_at is None:
        return False
    code, position = code_at
    if not 1 <= code <= 16:
        return False

    if position < len(data) and data[position] == 0x12:
        message_at = _length_delimited(data, position + 1)
        if message_at is None:
            return False
        message, position = message_at
        if not message or not _valid_utf8(message):
            return False

    while position < len(data) and data[position] == 0x1a:
        detail_at = _length_delimited(data, position + 1)
        if detail_at is None:
            return False
        detail, position = detail_at
        if not _canonical_any(detail):
            return False
    return position == len(data)


def _legacy_status_details_wire_value(value: bytes) -> bytes | None:
    """Recognise the encoded rich-status value emitted by the companion.

    The narrow wire-shape check keeps this compatibility independent of the
    optional protobuf packages without treating arbitrary base64 as encoded.
    """
    normalized = _normalized_base64(value)
    if normalized is None:
        return None
    decoded = base64.b64decode(normalized + b'=' * (-len(normalized) % 4))
    return normalized if _canonical_non_ok_status(decoded) else None


def _encode_outbound_metadata(
        metadata: list[tuple[bytes, bytes]]) -> list[tuple[bytes, bytes]]:
    """Convert application binary metadata to HTTP-safe gRPC field values.

    Context methods take raw bytes for ``-bin`` keys, matching grpcio.  The
    companion rich-status helper supplies its canonical field already encoded;
    recognising that one key preserves its wire value without making every
    application ``-bin`` field ambiguous.
    """
    encoded = []
    for name, value in metadata:
        if isinstance(name, bytes) and name.lower().endswith(b'-bin'):
            if name.lower() == b'grpc-status-details-bin':
                legacy_value = _legacy_status_details_wire_value(value)
                if legacy_value is not None:
                    encoded.append((name, legacy_value))
                    continue
            value = base64.b64encode(value).rstrip(b'=')
        encoded.append((name, value))
    return encoded


def _status_trailers(status: GrpcStatus, details: str,
                     extra: list[tuple[bytes, bytes]] | None = None
                     ) -> list[tuple[bytes, bytes]]:
    trailers = [(b'grpc-status', str(int(status)).encode('ascii'))]
    if details:
        trailers.append((b'grpc-message', _pct_encode_message(details)))
    if extra:
        trailers.extend(_encode_outbound_metadata(extra))
    return trailers


async def _send_trailers_only(send, status: GrpcStatus, details: str,
                              content_type: bytes = _GRPC_CONTENT_TYPE,
                              trailing: list[tuple[bytes, bytes]] | None = None
                              ) -> None:
    """Emit HTTP 200 with no message and status in END_STREAM trailing HEADERS.

    Preserve handler-set trailing metadata on errors, including details-bin.
    """
    await send(NativeResponse(
        status=200,
        header=[(b'content-type', content_type),
                (b'grpc-accept-encoding', _GRPC_ACCEPT_ENCODING)],
        expects_trailers=True))
    await send(NativeResponse(
        trailers=_status_trailers(status, details, trailing)))


async def _read_unary_request(receive, encoding: bytes) -> bytes:
    """Validate the complete input before invoking a single-request handler."""
    messages = _iter_request_messages(receive, encoding, single=True)
    request = None
    async for request in messages:
        pass
    if request is None:
        raise GrpcError(
            GrpcStatus.UNIMPLEMENTED,
            'method expects exactly 1 request message, got 0')
    return request


async def _iter_request_messages(receive, encoding: bytes, *, single: bool = False):
    """Bound each message before buffering its body; transport chunks stay borrowed."""
    buf = bytearray()
    seen = False
    try:
        async for chunk in stream_body(receive):
            offset = 0
            while offset < len(chunk):
                if len(buf) < _PREFIX_LEN:
                    if not buf and len(chunk) - offset >= _PREFIX_LEN:
                        flag, length = _PREFIX.unpack_from(chunk, offset)
                        prefix_end = offset + _PREFIX_LEN
                    else:
                        end = min(len(chunk), offset + _PREFIX_LEN - len(buf))
                        buf.extend(memoryview(chunk)[offset:end])
                        offset = end
                        if len(buf) < _PREFIX_LEN:
                            break
                        flag, length = _PREFIX.unpack_from(buf)
                        prefix_end = offset
                    if single and seen:
                        raise GrpcError(
                            GrpcStatus.UNIMPLEMENTED,
                            'method expects exactly 1 request message, got more')
                    limit = (MAX_MESSAGE_LENGTH if flag
                             else min(MAX_MESSAGE_SIZE, MAX_MESSAGE_LENGTH))
                    if length > limit:
                        raise GrpcError(
                            GrpcStatus.RESOURCE_EXHAUSTED,
                            f'request message ({length} bytes) larger than the '
                            f'{limit}-byte limit')
                    if not buf and len(chunk) - prefix_end >= length:
                        message = chunk[prefix_end:prefix_end + length]
                        offset = prefix_end + length
                    else:
                        if not buf:
                            buf.extend(memoryview(chunk)[offset:prefix_end])
                        offset = prefix_end
                if buf:
                    end = min(len(chunk), offset + _PREFIX_LEN + length - len(buf))
                    buf.extend(memoryview(chunk)[offset:end])
                    offset = end
                    if len(buf) < _PREFIX_LEN + length:
                        break
                    message = bytes(memoryview(buf)[_PREFIX_LEN:])
                    buf.clear()
                seen = True
                yield _decompress_message(message, encoding) if flag else message
    except ClientDisconnected as exc:
        raise GrpcError(GrpcStatus.CANCELLED, 'client disconnected mid-stream') from exc
    if buf:
        raise GrpcError(
            GrpcStatus.INTERNAL,
            f'malformed request: {len(buf)} trailing byte(s) after last message')


def _validate_response_message(response) -> bytes:
    """Return *response* as ``bytes`` or raise [`GrpcError`][] (INTERNAL for
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


def _response_headers(content_type: bytes,
                      response_encoding: bytes | None = None,
                      initial_metadata: list[tuple[bytes, bytes]] | None = None
                      ) -> list[tuple[bytes, bytes]]:
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
        headers.extend(_encode_outbound_metadata(initial_metadata))
    return headers


def _response_start(content_type: bytes,
                    response_encoding: bytes | None = None,
                    initial_metadata: list[tuple[bytes, bytes]] | None = None
                    ) -> NativeResponse:
    return NativeResponse(
        status=200,
        header=_response_headers(content_type, response_encoding,
                                 initial_metadata),
        expects_trailers=True)


def _check_deadline(context: GrpcContext) -> None:
    if context._deadline_expired or (context._deadline is not None
                                    and context._clock() >= context._deadline):
        raise _RpcDeadlineExceeded('deadline exceeded')


async def _serve_unary(handler, request, context, send, content_type,
                       response_encoding: bytes | None = None) -> None:
    """Run a unary handler and emit HEADERS → one DATA → status trailers.

    Normally nothing is written until the response is computed, so a failure is
    a clean Trailers-Only error.  A handler that called
    ``context.send_initial_metadata`` has already flushed HEADERS, so an error
    after that rides the trailing HEADERS frame instead — ``_finish_stream_error``
    picks the right shape from ``context._started``."""
    try:
        if context._deadline is not None:
            _check_deadline(context)
        response = await handler(request, context)
        response = _validate_response_message(response)
    except GrpcError as exc:
        await _finish_stream_error(
            send, context, exc.status, exc.details, content_type)
        return
    except Exception as exc:  # noqa: BLE001 — handler isolation
        # Handler errors are INTERNAL; task cancellation propagates.
        _check_deadline(context)
        logger.exception('gRPC unary handler raised')
        await _finish_stream_error(
            send, context, GrpcStatus.INTERNAL, str(exc), content_type)
        return

    body = _frame_response(response, response_encoding is not None)
    trailers = _status_trailers(context.code, context.details,
                                context._trailing)

    if context._started:
        # ``send_initial_metadata`` already put HEADERS on the wire, so only
        # the body and trailers are left.
        await send(NativeResponse(body=body, more_body=True))
        await send(NativeResponse(trailers=trailers))
        return

    # END_STREAM belongs on the trailers, not the body (RFC 9113 §8.1).
    if context._deadline is not None:
        _check_deadline(context)
    context._started = True
    await send(NativeResponse.with_trailers(
        200,
        _response_headers(content_type, response_encoding,
                          context._initial_metadata),
        body, trailers))


async def _serve_server_streaming(handler, request, context, send, content_type,
                                  response_encoding: bytes | None = None) -> None:
    """Drive a server-streaming (async-generator) handler.

    Headers are lazy; errors before the first message are Trailers-Only.
    Close the generator on exit; deadline cleanup is bounded.
    """
    agen = handler(request, context)
    # context._start_response is idempotent after early initial metadata.
    # Buffer already-length-prefixed messages independently of DATA boundaries.
    buf = bytearray()
    # Serialises concurrent flushes (the idle flusher below vs the drive
    # loop's batch flush): each flush snapshots-and-clears under the lock, so
    # message order on the wire matches yield order.
    flush_lock = asyncio.Lock()

    # Unannotated for the per-request-closure reason (see
    # app.py::_wrap_send_native); takes nothing, returns nothing.
    async def _flush():
        async with flush_lock:
            if not buf:
                return
            data = bytes(buf)
            buf.clear()
            await context._start_response()
            await send(NativeResponse(body=data, more_body=True))

    # Flush partial batches when the producer suspends; synchronous bursts
    # may continue batching. A parked producer must not withhold yielded messages.
    flush_wanted = asyncio.Event()
    finished = False

    # Unannotated for the per-request-closure reason (see
    # app.py::_wrap_send_native); takes nothing, returns nothing.
    async def _idle_flusher():
        while not finished:
            await flush_wanted.wait()
            flush_wanted.clear()
            await _flush()

    idle_flusher = asyncio.create_task(_idle_flusher())

    # Unannotated for the per-request-closure reason (see
    # app.py::_wrap_send_native); takes nothing, returns nothing.
    async def _stop_idle_flusher():
        # Cancel blocked writes on expiry; otherwise finish committed flushes.
        nonlocal finished
        finished = True
        flush_wanted.set()
        try:
            if asyncio.current_task().cancelling() or context.time_remaining() == 0:
                idle_flusher.cancel()
                await asyncio.gather(idle_flusher, return_exceptions=True)
            else:
                await idle_flusher
        except Exception:  # noqa: BLE001 — send failures already surfaced
            logger.exception('gRPC stream idle flusher raised')

    # Unannotated for the per-request-closure reason (see
    # app.py::_wrap_send_native); takes nothing, returns nothing.
    async def _drive():
        it = agen.__aiter__()
        while True:
            if context._deadline is not None:
                _check_deadline(context)
            try:
                msg = await it.__anext__()
            except StopAsyncIteration:
                return
            buf.extend(_frame_response(_validate_response_message(msg),
                                       response_encoding is not None))
            if len(buf) >= _STREAM_BATCH_BYTES:
                await _flush()
            elif buf:
                flush_wanted.set()

    try:
        await _drive()
    except GrpcError as exc:
        # Deliver messages already committed by the generator, then the status.
        await _flush()
        await _finish_stream_error(
            send, context, exc.status, exc.details, content_type)
        return
    except Exception as exc:  # noqa: BLE001 — handler isolation
        # Preserve messages already yielded before a handler failure.
        _check_deadline(context)
        await _flush()
        logger.exception('gRPC server-streaming handler raised')
        await _finish_stream_error(
            send, context, GrpcStatus.INTERNAL, str(exc), content_type)
        return
    finally:
        # Expiry discards buffered messages; cleanup must preserve the reported status.
        try:
            await _stop_idle_flusher()
        finally:
            try:
                if asyncio.current_task().cancelling() or context.time_remaining() == 0:
                    async with asyncio.timeout(_TERMINATION_TIMEOUT):
                        await agen.aclose()
                else:
                    await agen.aclose()
            except Exception:  # noqa: BLE001
                logger.exception('gRPC server-streaming generator aclose() raised')

    # Success: flush the final partial batch, then the OK trailer.  For an empty
    # stream (nothing buffered / yielded) _flush is a no-op and the headers are
    # sent here so the client still sees Response-Headers + Trailers.
    await _flush()
    await context._start_response()  # idempotent — emits HEADERS if not yet sent
    await send(NativeResponse(
        trailers=_status_trailers(context.code, context.details,
                                  context._trailing)))


async def _finish_stream_error(send, context: GrpcContext, status: GrpcStatus,
                               details: str, content_type: bytes) -> None:
    """Report a handler error: in trailers if Response-Headers already went
    out (``context._started``), otherwise as a Trailers-Only response.

    Either way the context's trailing metadata is delivered with the status —
    grpcio's contract for ``set_trailing_metadata`` on an aborted call, and
    the transport leg of the rich-error model (``grpc-status-details-bin``)."""
    if context._started:
        await send(NativeResponse(
            trailers=_status_trailers(status, details, context._trailing)))
    else:
        await _send_trailers_only(send, status, details, content_type,
                                  context._trailing)


async def _serve_call(method, context, receive, send, content_type,
                     request_encoding, response_encoding):
    if context._deadline is not None:
        _check_deadline(context)
    if method.client_streaming:
        request = _iter_request_messages(receive, request_encoding)
    else:
        try:
            request = await _read_unary_request(receive, request_encoding)
        except GrpcError as exc:
            await _send_trailers_only(send, exc.status, exc.details, content_type)
            return
    try:
        if method.streaming:
            await _serve_server_streaming(
                method.handler, request, context, send, content_type, response_encoding)
        else:
            await _serve_unary(
                method.handler, request, context, send, content_type, response_encoding)
    finally:
        if method.client_streaming:
            await request.aclose()


async def _serve_with_deadline(call, context):
    task = create_eager_task(call)
    if task.done():
        return task.result()
    budget = asyncio.timeout_at(context._deadline)
    try:
        async with budget:
            await asyncio.shield(task)
    except BaseException as failure:
        if budget.expired():
            context._deadline_expired = True
        task.cancel()
        try:
            # Cancellation cleanup has its own finite budget.
            async with asyncio.timeout(_TERMINATION_TIMEOUT):
                await task
        except (asyncio.CancelledError, TimeoutError):
            if asyncio.current_task().cancelling():
                raise asyncio.CancelledError from None
        except Exception:  # noqa: BLE001 — preserve the original failure
            logger.exception('gRPC cancellation cleanup raised')
        if budget.expired() and isinstance(failure, TimeoutError):
            raise _RpcDeadlineExceeded from None
        raise


async def serve_grpc(registry: GrpcServiceRegistry, conn, receive, send) -> None:
    """Serve any of the four gRPC call shapes through Connection/receive/send.

    Report handler and protocol failures as gRPC status.
    """
    path = _req_field(conn, 'path', '')
    context = GrpcContext(conn)
    # Echo the request's content-type subtype (application/grpc+proto, +json, …)
    # back on the response, defaulting to bare application/grpc.
    content_type = _resolve_content_type(context.metadata(b'content-type'))

    method = registry.lookup_method(path)
    if method is None:
        await _send_trailers_only(
            send, GrpcStatus.UNIMPLEMENTED, f'Method not found: {path}', content_type)
        return

    # RFC: enforce the client's deadline (grpc-timeout) if it sent one.
    duration = _parse_grpc_timeout(context.metadata(b'grpc-timeout'))
    deadline = asyncio.get_running_loop().time() + duration if duration is not None else None

    # Compression negotiation.  The request's ``grpc-encoding`` names the coding
    # of its compressed messages; the client's ``grpc-accept-encoding`` says
    # what it can decode, so we may gzip responses only when it lists gzip.
    request_encoding = context.metadata(b'grpc-encoding').strip().lower()
    response_encoding = (
        b'gzip' if _accepts_gzip(context.metadata(b'grpc-accept-encoding'))
        else None)

    # Wire the context's response side now, so the handler can call
    # context.send_initial_metadata / time_remaining before any bytes go out.
    response_send = send
    if deadline is not None:
        async def deadline_send(event):
            _check_deadline(context)
            await send(event)
        response_send = deadline_send
    context._bind(response_send, content_type, response_encoding, deadline)

    call = _serve_call(method, context, receive, response_send, content_type,
                       request_encoding, response_encoding)
    try:
        if deadline is None:
            await call
        else:
            await _serve_with_deadline(call, context)
    except _RpcDeadlineExceeded:
        context._deadline_expired = True
        try:
            # Expiry reporting has a separate bounded budget, never renewed RPC work.
            async with asyncio.timeout(_TERMINATION_TIMEOUT):
                await _finish_stream_error(send, context, GrpcStatus.DEADLINE_EXCEEDED,
                                           'deadline exceeded', content_type)
        except TimeoutError:
            raise TimeoutError('gRPC deadline notification blocked') from None
