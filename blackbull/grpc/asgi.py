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
    """Emit a Trailers-Only-style error: HTTP 200 + grpc-status in the
    response headers and an empty, end-of-stream body (no message)."""
    headers = [(b'content-type', content_type),
               (b'grpc-accept-encoding', _GRPC_ACCEPT_ENCODING)]
    headers.extend(_status_trailers(status, details))
    await send({'type': 'http.response.start', 'status': 200,
                'headers': headers})
    await send({'type': 'http.response.body', 'body': b'', 'more_body': False})


async def serve_grpc(registry: GrpcServiceRegistry, scope, receive, send) -> None:
    """Serve a single unary gRPC call through the ASGI (scope, receive, send)
    bridge.  Never raises: every failure path is reported as a gRPC status."""
    path = scope.get('path', '')
    context = GrpcContext(scope)
    # Echo the request's content-type subtype (application/grpc+proto, +json, …)
    # back on the response, defaulting to bare application/grpc.
    content_type = _resolve_content_type(context.metadata(b'content-type'))

    handler = registry.lookup(path)
    if handler is None:
        await _send_trailers_only(
            send, GrpcStatus.UNIMPLEMENTED, f'Method not found: {path}', content_type)
        return

    try:
        body = await read_body(receive)
        try:
            messages = decode_messages(body)
        except GrpcDecodeError as exc:
            raise GrpcError(GrpcStatus.INTERNAL, f'malformed request: {exc}')
        if len(messages) != 1:
            raise GrpcError(
                GrpcStatus.UNIMPLEMENTED,
                f'unary method expects exactly 1 request message, got {len(messages)}')
        compressed, request = messages[0]
        if compressed:
            raise GrpcError(
                GrpcStatus.UNIMPLEMENTED, 'message compression is not supported')
        if len(request) > MAX_MESSAGE_SIZE:
            raise GrpcError(
                GrpcStatus.RESOURCE_EXHAUSTED,
                f'request message ({len(request)} bytes) larger than the '
                f'{MAX_MESSAGE_SIZE}-byte limit')

        # RFC: enforce the client's deadline (grpc-timeout) if it sent one.
        deadline = _parse_grpc_timeout(context.metadata(b'grpc-timeout'))
        if deadline is not None:
            response = await asyncio.wait_for(
                handler(request, context), timeout=deadline)
        else:
            response = await handler(request, context)
    except GrpcError as exc:
        await _send_trailers_only(send, exc.status, exc.details, content_type)
        return
    except asyncio.TimeoutError:
        # grpc-timeout exceeded — report DEADLINE_EXCEEDED, not INTERNAL.
        await _send_trailers_only(
            send, GrpcStatus.DEADLINE_EXCEEDED, 'deadline exceeded', content_type)
        return
    except (asyncio.CancelledError, KeyboardInterrupt, SystemExit):
        # Cooperative cancellation (RST_STREAM CANCEL, shutdown) and process-exit
        # signals must propagate — never swallow them as a gRPC status.
        raise
    except BaseException as exc:  # noqa: BLE001 — handler isolation: a handler
        # bug (even a bare BaseException) must report INTERNAL, not kill the stream.
        logger.exception('gRPC handler raised for %s', path)
        await _send_trailers_only(send, GrpcStatus.INTERNAL, str(exc), content_type)
        return

    if not isinstance(response, (bytes, bytearray)):
        await _send_trailers_only(
            send, GrpcStatus.INTERNAL,
            f'handler returned {type(response).__name__}, expected bytes', content_type)
        return

    # Success: response HEADERS, one message DATA frame, then status trailers.
    await send({'type': 'http.response.start', 'status': 200,
                'headers': [(b'content-type', content_type),
                            (b'grpc-accept-encoding', _GRPC_ACCEPT_ENCODING)],
                'trailers': True})
    await send({'type': 'http.response.body',
                'body': encode_message(bytes(response)), 'more_body': True})
    await send({'type': 'http.response.trailers',
                'headers': _status_trailers(context.code, context.details,
                                            context._trailing)})
