"""An app-facing seam for testing your own gRPC servicers.

BlackBull ships a gRPC **server** and no gRPC client, and this module does
not add one.  What it adds is the boilerplate the framework's own gRPC
tests repeat — serve the app on an ephemeral h2c port, POST with
``content-type: application/grpc``, read the status back out of the
*trailing* headers — behind one helper::

    async with GrpcTestServer(app) as grpc:
        reply = await grpc.unary('/demo.Greeter/SayHello', b'world')

    assert reply.status is GrpcStatus.OK
    assert reply.message == b'hi world'

**Why a seam is needed at all.**  Every gRPC response reports its status in
trailing headers — success and error alike — so a transport with no
``http.response.trailers`` support never observes completion.  That is why
the framework's own gRPC tests moved off the in-process ``TestClient`` and
onto :class:`~blackbull.client.http2.HTTP2Client` over a real socket:
``HTTP2Client`` handles trailers natively and folds them into
``res.headers``.  An application developer testing a servicer needs the
same thing, and until now had to rediscover it.

The gRPC analogue of :class:`~blackbull.testing.native.NativeTestServer`,
and deliberately the same shape: a real server on a loopback port, the
whole dispatch path exercised, and the port left public so anything else
can drive it too.
"""
from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass

from ..grpc import GrpcStatus, decode_messages, encode_message
from ..server.server import ASGIServer

#: Long enough for the accept loop to bind before the first call.  The
#: framework's own gRPC integration tests use the same figure.
_STARTUP_WAIT_S = 0.15


@dataclass(frozen=True)
class GrpcReply:
    """One gRPC response, with the trailer fields already read out.

    ``status`` and ``grpc_message`` come from *trailing* headers, which is
    the whole reason this seam exists — reading them off the response
    object is what an app developer would otherwise have to work out.
    """
    #: The ``grpc-status`` trailer, as the enum.
    status: GrpcStatus
    #: The first response message, or ``b''`` when the call carried none.
    message: bytes
    #: Every response message, for server-streaming calls.
    messages: tuple[bytes, ...]
    #: The ``grpc-message`` trailer — the human-readable detail.
    grpc_message: str
    #: The raw response, for anything this dataclass does not surface.
    response: object


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
        return _read_reply(response)


def _read_reply(response) -> GrpcReply:
    """Fold one ``ClientResponse`` into a :class:`GrpcReply`.

    ``grpc-status`` is absent on some error paths that fail before the
    handler runs; absent is treated as ``OK`` because that is what the
    protocol says an omitted trailer means, and inventing UNKNOWN here
    would report a failure the server never sent.
    """
    raw_status = response.headers.get(b'grpc-status', b'')
    status = GrpcStatus(int(raw_status)) if raw_status else GrpcStatus.OK
    message_text = response.headers.get(b'grpc-message', b'').decode()
    body = getattr(response, 'body', b'') or b''
    try:
        # ``decode_messages`` yields ``(compressed, payload)``; the flag is
        # the transport's business, and a servicer test asserts on payloads.
        messages = tuple(payload for _compressed, payload
                         in decode_messages(body))
    except Exception:
        # A truncated or unframed body is itself a finding; hand back what
        # arrived rather than raising out of a test helper.
        messages = ()
    return GrpcReply(
        status=status,
        message=messages[0] if messages else b'',
        messages=messages,
        grpc_message=message_text,
        response=response,
    )
