"""gRPC server over HTTP/2.

Enable with app.enable_grpc(registry). Handlers exchange raw message bytes;
protobuf is optional. Streaming contracts are in docs/guide/grpc.md.
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
