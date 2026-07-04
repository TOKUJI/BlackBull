"""Pure-Python gRPC server support for BlackBull.

gRPC runs on HTTP/2; BlackBull already ships a complete HTTP/2 implementation,
so this package adds only the gRPC-specific pieces:

- :mod:`~blackbull.grpc.codec` — Length-Prefixed-Message framing.
- :class:`~blackbull.grpc.registry.GrpcServiceRegistry` — ``/Service/Method`` → handler.
- :class:`~blackbull.grpc.status.GrpcStatus` / :class:`GrpcError` — canonical codes.
- :func:`~blackbull.grpc.asgi.serve_grpc` + :class:`GrpcContext` — the ASGI bridge.

Wire it into an app with ``app.enable_grpc(registry)``; gRPC requests
(``content-type: application/grpc``) are then multiplexed onto the same
HTTP/2 port as the app's REST and WebSocket traffic.  All four RPC kinds are
served — unary, server-streaming, client-streaming, and bidirectional — where a
response-streaming handler is an async generator and a request-streaming handler
takes an async iterator of request messages (``request_iter``).

Protobuf is **not** a dependency: handlers receive and return raw message
bytes, so the application chooses its own serialisation (``grpc_tools.protoc``
output, ``protobuf``, or hand-rolled).
"""
from .codec import encode_message, decode_messages, GrpcDecodeError
from .registry import GrpcServiceRegistry
from .status import GrpcStatus, GrpcError
from .asgi import serve_grpc, GrpcContext

__all__ = [
    'encode_message', 'decode_messages', 'GrpcDecodeError',
    'GrpcServiceRegistry', 'GrpcStatus', 'GrpcError',
    'serve_grpc', 'GrpcContext',
]
