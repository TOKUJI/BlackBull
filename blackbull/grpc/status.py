"""gRPC canonical status codes and the error type handlers raise.

The codes are the gRPC/google.rpc canonical set
(https://grpc.github.io/grpc/core/md_doc_statuscodes.html).  They travel on
the wire as the decimal ``grpc-status`` trailer; ``OK`` (0) signals success.
"""
from __future__ import annotations

from enum import IntEnum


class GrpcStatus(IntEnum):
    OK = 0
    CANCELLED = 1
    UNKNOWN = 2
    INVALID_ARGUMENT = 3
    DEADLINE_EXCEEDED = 4
    NOT_FOUND = 5
    ALREADY_EXISTS = 6
    PERMISSION_DENIED = 7
    RESOURCE_EXHAUSTED = 8
    FAILED_PRECONDITION = 9
    ABORTED = 10
    OUT_OF_RANGE = 11
    UNIMPLEMENTED = 12
    INTERNAL = 13
    UNAVAILABLE = 14
    DATA_LOSS = 15
    UNAUTHENTICATED = 16


class GrpcError(Exception):
    """Raised by a service handler (or ``context.abort``) to end the RPC with
    a non-OK status.  ``serve_grpc`` translates it into ``grpc-status`` /
    ``grpc-message`` trailers."""

    def __init__(self, status: GrpcStatus, details: str = ''):
        # The status is always produced locally (a handler raising, or
        # serve_grpc mapping an exception) — it never arrives from the wire,
        # since grpc-status is a response-only trailer.  So, like grpcio's
        # enum-typed ServicerContext API, the contract is the GrpcStatus enum;
        # a raw/out-of-range int is a caller bug caught at the type boundary.
        # GrpcStatus() stays as a production backstop (raises ValueError on a
        # bare int when beartype is not instrumenting).
        self.status = GrpcStatus(status)
        self.details = details
        super().__init__(f'{self.status.name}: {details}' if details else self.status.name)
