"""Loopback gRPC test server.

Status lives in trailers, so an in-process ASGI transport that loses trailers
cannot establish gRPC completion. This helper does not provide a general gRPC client.
"""
from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass
from operator import itemgetter
from urllib.parse import unquote_to_bytes

from ..grpc import GrpcDecodeError, GrpcStatus, decode_messages, encode_message
from ..grpc.asgi import _is_grpc_content_type
from ..server.server import ASGIServer

#: Long enough for the accept loop to bind before the first call.  The
#: framework's own gRPC integration tests use the same figure.
_STARTUP_WAIT_S = 0.15

#: http-grpc-status-mapping.md, for a reply with no grpc-status; any other
#: HTTP status maps to UNKNOWN.
_HTTP_STATUS_TO_GRPC = {
    400: GrpcStatus.INTERNAL,
    401: GrpcStatus.UNAUTHENTICATED,
    403: GrpcStatus.PERMISSION_DENIED,
    404: GrpcStatus.UNIMPLEMENTED,
    429: GrpcStatus.UNAVAILABLE,
    502: GrpcStatus.UNAVAILABLE,
    503: GrpcStatus.UNAVAILABLE,
    504: GrpcStatus.UNAVAILABLE,
}

#: Every valid grpc-status value: a defined code, decimal, no leading zeros.
_STATUS_BY_WIRE = {str(status.value).encode(): status for status in GrpcStatus}

_flag = itemgetter(0)
_payload = itemgetter(1)


@dataclass(frozen=True)
class GrpcReply:
    """One gRPC call's reply, judged as a gRPC client must (PROTOCOL-HTTP2.md)."""
    #: The server's ``grpc-status``, or one synthesized for a malformed reply,
    #: which is never ``OK``.
    status: GrpcStatus
    #: The first response message, or ``b''`` when the call carried none.
    message: bytes
    #: Every response message, for server-streaming calls.
    messages: tuple[bytes, ...]
    #: The ``grpc-message`` field, percent-decoded.
    grpc_message: str
    #: The raw response, for anything this dataclass does not surface.
    response: object
    #: ``None`` for a well-formed reply; otherwise what is wrong with it.  A
    #: valid non-``OK`` ``grpc-status`` is still kept as ``status``.
    violation: str | None = None


class GrpcTestServer:
    """Serve *app* on an ephemeral h2c port and call its gRPC methods.

    ``async with`` only: the server shares the test's event loop, as it
    shares the process loop in production.
    """

    def __init__(self, app, *, host: str = '127.0.0.1') -> None:
        self._app = app
        self.host = host
        self.port: int = 0
        self._server: ASGIServer | None = None
        self._task: asyncio.Task | None = None

    async def __aenter__(self) -> 'GrpcTestServer':
        self._server = ASGIServer(self._app)
        self._server.open_socket(port=0)
        self._task = asyncio.create_task(self._server.run())
        await asyncio.sleep(_STARTUP_WAIT_S)
        self.port = self._server.port
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        if self._task is not None:
            self._task.cancel()
            # We cancelled it, so ``CancelledError`` is the expected answer
            # and not an error; anything else the serve loop raises on its
            # way down is a teardown detail that must not replace whatever
            # the test was actually asserting.  ``contextlib.suppress`` is
            # the idiom the rest of the tree uses for exactly this.
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task
            self._task = None
        self._server = None

    async def unary(self, method: str, request: bytes = b'', *,
                    metadata: list[tuple[str, str]] | None = None,
                    timeout: float = 5.0) -> GrpcReply:
        """Call *method* with one request message and read the reply.

        The length-prefixed framing is applied for you: pass the message
        bytes your servicer expects to receive, not an encoded frame.
        """
        from ..client.http2 import HTTP2Client  # noqa: PLC0415

        headers = [('content-type', 'application/grpc')] + list(metadata or [])
        async with HTTP2Client(self.host, self.port) as client:
            response = await asyncio.wait_for(
                client.request('POST', method, headers=headers,
                               body=encode_message(request)),
                timeout=timeout)
        registry = getattr(self._app, '_grpc_registry', None)
        shape = registry.lookup_method(method) if registry is not None else None
        return _read_reply(response,
                           unary=None if shape is None else not shape.streaming)


def _read_reply(response, *, unary: bool | None = None) -> GrpcReply:
    """Judge *response* as the reply to one gRPC call (PROTOCOL-HTTP2.md).

    *unary* is the call's response shape when known: a unary ``OK`` reply
    must carry exactly one message.  ``None`` skips that check.
    """
    # A Trailers-Only response carries its status in the head.
    section = response.headers if response.ended_on_head else response.trailers
    fields = section.getlist(b'grpc-status')
    server_status = _STATUS_BY_WIRE.get(fields[0][1]) if len(fields) == 1 else None
    problems = [] if server_status is not None else [_status_problem(fields)]
    transport = response.status != 200
    if transport:
        problems.append(f'HTTP status {response.status} (expected 200)')
    content_type = response.headers.get(b'content-type', b'')
    if not _is_grpc_content_type(content_type):
        transport = True
        problems.append(f'content-type {content_type!r} is not application/grpc')
    messages: tuple[bytes, ...] = ()
    try:
        frames = decode_messages(response.body)
    except GrpcDecodeError as exc:
        problems.append(f'message framing: {exc}')
    else:
        messages = tuple(map(_payload, frames))
        if any(map(_flag, frames)):
            # The call announces no grpc-accept-encoding.
            problems.append('a compressed message, but the call accepts identity only')
        elif unary and server_status is GrpcStatus.OK and len(messages) != 1:
            problems.append(f'a unary reply carried {len(messages)} messages (expected 1)')

    if server_status is None:
        # The HTTP mapping covers only a missing grpc-status.
        status = (GrpcStatus.UNKNOWN if fields
                  else _HTTP_STATUS_TO_GRPC.get(response.status, GrpcStatus.UNKNOWN))
    elif server_status is not GrpcStatus.OK or not problems:
        status = server_status
    elif transport:
        status = _HTTP_STATUS_TO_GRPC.get(response.status, GrpcStatus.UNKNOWN)
    else:
        status = GrpcStatus.INTERNAL
    raw_message = section.get(b'grpc-message', b'')
    if b'%' in raw_message:
        # Invalid %-encodings stay as they are (PROTOCOL-HTTP2.md).
        raw_message = unquote_to_bytes(raw_message)
    return GrpcReply(
        status=status,
        message=messages[0] if messages else b'',
        messages=messages,
        grpc_message=raw_message.decode('utf-8', 'replace'),
        response=response,
        violation='; '.join(problems) if problems else None,
    )


def _status_problem(fields: list[tuple[bytes, bytes]]) -> str:
    """Why *fields* holds no valid ``grpc-status``."""
    if not fields:
        return 'no grpc-status'
    if len(fields) > 1:
        return f'{len(fields)} grpc-status fields'
    return f'grpc-status {fields[0][1]!r} is not a defined code in canonical decimal'
