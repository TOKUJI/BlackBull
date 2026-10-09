"""Loopback gRPC test server.

Status lives in trailers, so an in-process ASGI transport that loses trailers
cannot establish gRPC completion. This helper does not provide a general gRPC client.
"""
from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass
import urllib.parse

from ..grpc import GrpcDecodeError, GrpcStatus, decode_messages, encode_message
from ..server.server import ASGIServer

#: Long enough for the accept loop to bind before the first call.  The
#: framework's own gRPC integration tests use the same figure.
_STARTUP_WAIT_S = 0.15

#: gRPC's http-grpc-status-mapping.md, for a response with no grpc-status;
#: every other HTTP status maps to UNKNOWN.
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


@dataclass(frozen=True)
class GrpcReply:
    """One gRPC response, with the trailer fields already read out.

    ``status`` and ``grpc_message`` come from *trailing* headers, which is
    the whole reason this seam exists — reading them off the response
    object is what an app developer would otherwise have to work out.
    """
    #: The call's status: the server's ``grpc-status``, or one synthesized
    #: for a malformed response (see ``violation``).  Never ``OK`` for a
    #: malformed response.
    status: GrpcStatus
    #: The first response message, or ``b''`` when the call carried none.
    message: bytes
    #: Every response message, for server-streaming calls.
    messages: tuple[bytes, ...]
    #: The ``grpc-message`` trailer, percent-decoded — the human-readable detail.
    grpc_message: str
    #: The raw response, for anything this dataclass does not surface.
    response: object
    #: ``None`` for a well-formed gRPC response; otherwise what was wrong
    #: with it.  ``status`` is then synthesized, except that a valid non-OK
    #: ``grpc-status`` from the server is kept.
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
    # A Trailers-Only response carries its status in the head; any other
    # response carries it only in the trailer section.
    trailers_only = not response.trailers and not response.body
    section = response.headers if trailers_only else response.trailers
    status_values = [value for _, value in section.getlist(b'grpc-status')]
    server_status, status_problem = _parse_status(status_values)

    transport: list[str] = []
    if response.status != 200:
        transport.append(f'HTTP status {response.status} (expected 200)')
    content_type = response.headers.get(b'content-type', b'').lower()
    if content_type != b'application/grpc' \
            and not content_type.startswith(b'application/grpc+'):
        transport.append(f'content-type {content_type!r} is not application/grpc')

    content: list[str] = []
    messages: tuple[bytes, ...] = ()
    try:
        frames = decode_messages(response.body)
    except GrpcDecodeError as exc:
        content.append(f'message framing: {exc}')
    else:
        messages = tuple(payload for _, payload in frames)
        if any(compressed for compressed, _ in frames):
            # This client announces no grpc-accept-encoding (compression.md).
            content.append('a compressed message, but the call accepts '
                           'identity only')
        elif unary and server_status is GrpcStatus.OK and len(messages) != 1:
            content.append(f'a unary reply carried {len(messages)} messages '
                           f'(expected 1)')

    if server_status is None:
        # The HTTP mapping covers only a missing grpc-status.
        status = (GrpcStatus.UNKNOWN if status_values
                  else _HTTP_STATUS_TO_GRPC.get(response.status, GrpcStatus.UNKNOWN))
    elif server_status is not GrpcStatus.OK or not (transport or content):
        status = server_status
    elif transport:
        status = _HTTP_STATUS_TO_GRPC.get(response.status, GrpcStatus.UNKNOWN)
    else:
        status = GrpcStatus.INTERNAL
    problems = ([status_problem] if status_problem else []) + transport + content
    raw_message = section.get(b'grpc-message', b'')
    return GrpcReply(
        status=status,
        message=messages[0] if messages else b'',
        messages=messages,
        # Invalid %-encodings stay as they are (PROTOCOL-HTTP2.md).
        grpc_message=urllib.parse.unquote_to_bytes(raw_message).decode(
            'utf-8', 'replace'),
        response=response,
        violation='; '.join(problems) or None,
    )


def _parse_status(values: list[bytes]) -> tuple[GrpcStatus | None, str | None]:
    """Return ``(status, None)`` for one valid ``grpc-status`` field value,
    or ``(None, problem)``."""
    if not values:
        return None, 'no grpc-status'
    if len(values) > 1:
        return None, f'{len(values)} grpc-status fields'
    raw = values[0]
    # 1*DIGIT without leading zeros; bytes.isdigit() is ASCII-only.
    if not raw.isdigit() or (len(raw) > 1 and raw.startswith(b'0')):
        return None, f'grpc-status {raw!r} is not a decimal code'
    try:
        return GrpcStatus(int(raw)), None
    except ValueError:
        return None, f'grpc-status {raw.decode()} is not a defined code'
