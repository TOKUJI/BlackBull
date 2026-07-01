"""ASGI bridge that serves unary gRPC calls over BlackBull's HTTP/2 layer.

gRPC is HTTP/2 with a fixed request shape (``POST /package.Service/Method``,
``content-type: application/grpc``) and a Length-Prefixed-Message body, where
the call result is reported in ``grpc-status`` / ``grpc-message`` *trailers*.
BlackBull's HTTP/2 sender already emits trailers via the
``http.response.trailers`` ASGI event, so a gRPC unary call maps cleanly onto
the existing (scope, receive, send) bridge — no new protocol Actor is needed.

``serve_grpc`` is dispatched from :meth:`BlackBull._dispatch` when the request
content-type is ``application/grpc`` and a registry was installed via
``app.enable_grpc(...)``.
"""
from __future__ import annotations

import asyncio
import logging
import os

from ..request import read_body
from .codec import decode_messages, encode_message, GrpcDecodeError
from .registry import GrpcServiceRegistry
from .status import GrpcError, GrpcStatus

logger = logging.getLogger(__name__)

_GRPC_CONTENT_TYPE = b'application/grpc'

# We do not implement message compression, but the spec says a server SHOULD
# advertise what it accepts so clients send uncompressed messages.
_GRPC_ACCEPT_ENCODING = b'identity'

# Per-message size cap at the gRPC layer (RESOURCE_EXHAUSTED above it).  4 MiB
# matches grpcio's default receive limit; override with BB_GRPC_MAX_MESSAGE_SIZE.
# Read at import; tests may monkeypatch this module attribute.
try:
    MAX_MESSAGE_SIZE = int(os.environ.get('BB_GRPC_MAX_MESSAGE_SIZE', 4 * 1024 * 1024))
except ValueError:
    MAX_MESSAGE_SIZE = 4 * 1024 * 1024

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


class GrpcContext:
    """Per-call context handed to a gRPC handler.

    Exposes request metadata (the HTTP/2 headers) and lets the handler set
    the outgoing status, a human-readable message, and trailing metadata, or
    abort the call outright.
    """

    __slots__ = ('scope', 'code', 'details', '_trailing')

    def __init__(self, scope: dict):
        self.scope = scope
        self.code: GrpcStatus = GrpcStatus.OK
        self.details: str = ''
        self._trailing: list[tuple[bytes, bytes]] = []

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

    def set_code(self, status: GrpcStatus) -> None:
        # Enum-only, matching grpcio's ServicerContext.set_code (see
        # GrpcError.__init__ for why a raw int is never a valid input here).
        self.code = GrpcStatus(status)

    def set_details(self, details: str) -> None:
        self.details = details

    def set_trailing_metadata(self, metadata) -> None:
        self._trailing = [(k, v) for k, v in metadata]

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


async def _read_unary_request(receive) -> bytes:
    """Read the request body and return the single de-framed request message.

    Server-streaming still takes exactly one request message (only the response
    streams), so unary and server-streaming share this.  Raises
    :class:`GrpcError` on a malformed / multi-message / compressed / oversized
    request."""
    body = await read_body(receive)
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
        raise GrpcError(
            GrpcStatus.UNIMPLEMENTED, 'message compression is not supported')
    if len(request) > MAX_MESSAGE_SIZE:
        raise GrpcError(
            GrpcStatus.RESOURCE_EXHAUSTED,
            f'request message ({len(request)} bytes) larger than the '
            f'{MAX_MESSAGE_SIZE}-byte limit')
    return request


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


def _response_start(content_type: bytes) -> dict:
    return {'type': 'http.response.start', 'status': 200,
            'headers': [(b'content-type', content_type),
                        (b'grpc-accept-encoding', _GRPC_ACCEPT_ENCODING)],
            'trailers': True}


async def _serve_unary(handler, request, context, send, content_type,
                       deadline: float | None) -> None:
    """Run a unary handler and emit HEADERS → one DATA → status trailers.

    Nothing is written until the response is computed, so any failure is
    reported as a clean Trailers-Only error by the caller."""
    try:
        if deadline is not None:
            response = await asyncio.wait_for(
                handler(request, context), timeout=deadline)
        else:
            response = await handler(request, context)
        response = _validate_response_message(response)
    except GrpcError as exc:
        await _send_trailers_only(send, exc.status, exc.details, content_type)
        return
    except asyncio.TimeoutError:
        await _send_trailers_only(
            send, GrpcStatus.DEADLINE_EXCEEDED, 'deadline exceeded', content_type)
        return
    except Exception as exc:  # noqa: BLE001 — handler isolation
        # Isolate handler bugs as INTERNAL.  CancelledError / KeyboardInterrupt /
        # SystemExit / GeneratorExit derive from BaseException (not Exception),
        # so they propagate here rather than being masked — task cancellation and
        # interpreter shutdown must never be turned into a gRPC status.
        logger.exception('gRPC unary handler raised')
        await _send_trailers_only(send, GrpcStatus.INTERNAL, str(exc), content_type)
        return

    await send(_response_start(content_type))
    await send({'type': 'http.response.body',
                'body': encode_message(response), 'more_body': True})
    await send({'type': 'http.response.trailers',
                'headers': _status_trailers(context.code, context.details,
                                            context._trailing)})


async def _serve_server_streaming(handler, request, context, send, content_type,
                                  deadline: float | None) -> None:
    """Drive a server-streaming (async-generator) handler.

    Response-Headers are sent lazily, just before the first message, so a
    handler that errors *before* yielding anything still produces a clean
    Trailers-Only error (like a unary failure).  Once a message has gone out the
    status can only ride the trailing HEADERS frame, so a mid-stream error is
    reported there.  The generator is always finalised (``aclose``) — including
    on client cancellation — so its ``finally``/cleanup runs."""
    agen = handler(request, context)
    started = False

    async def _emit(msg) -> None:
        nonlocal started
        payload = _validate_response_message(msg)
        if not started:
            await send(_response_start(content_type))
            started = True
        await send({'type': 'http.response.body',
                    'body': encode_message(payload), 'more_body': True})

    try:
        if deadline is not None:
            async with asyncio.timeout(deadline):
                async for msg in agen:
                    await _emit(msg)
        else:
            async for msg in agen:
                await _emit(msg)
    except GrpcError as exc:
        await _finish_stream_error(send, started, exc.status, exc.details, content_type)
        return
    except (asyncio.TimeoutError, TimeoutError):
        await _finish_stream_error(
            send, started, GrpcStatus.DEADLINE_EXCEEDED, 'deadline exceeded',
            content_type)
        return
    except Exception as exc:  # noqa: BLE001 — handler isolation
        # Isolate handler bugs as INTERNAL.  CancelledError / KeyboardInterrupt /
        # SystemExit / GeneratorExit derive from BaseException (not Exception),
        # so they propagate here — client cancellation still unwinds the stream
        # (and finalises the generator via the finally below) instead of being
        # reported as a gRPC status.
        logger.exception('gRPC server-streaming handler raised')
        await _finish_stream_error(
            send, started, GrpcStatus.INTERNAL, str(exc), content_type)
        return
    finally:
        # Finalise the generator so its cleanup runs on every exit path
        # (normal completion, error, or client cancellation).  Best-effort:
        # a raising aclose() must not mask the status we already reported.
        try:
            await agen.aclose()
        except Exception:  # noqa: BLE001
            logger.exception('gRPC server-streaming generator aclose() raised')

    # Success.  For an empty stream (no message ever yielded) send the headers
    # now so the client sees a well-formed Response-Headers + Trailers response.
    if not started:
        await send(_response_start(content_type))
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

    # Front matter (request read + de-framing) — errors here precede any
    # response bytes, so a clean Trailers-Only error is always valid.
    try:
        request = await _read_unary_request(receive)
    except GrpcError as exc:
        await _send_trailers_only(send, exc.status, exc.details, content_type)
        return

    # RFC: enforce the client's deadline (grpc-timeout) if it sent one.
    deadline = _parse_grpc_timeout(context.metadata(b'grpc-timeout'))

    if method.streaming:
        await _serve_server_streaming(
            method.handler, request, context, send, content_type, deadline)
    else:
        await _serve_unary(
            method.handler, request, context, send, content_type, deadline)
