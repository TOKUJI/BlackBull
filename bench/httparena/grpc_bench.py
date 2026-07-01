"""HttpArena ``unary-grpc`` profile — ``benchmark.BenchmarkService/GetSum``.

The proto is tiny::

    service BenchmarkService { rpc GetSum (SumRequest) returns (SumReply); }
    message SumRequest { int32 a = 1; int32 b = 2; }
    message SumReply   { int32 result = 1; }

BlackBull's gRPC bridge hands the handler the *de-framed* request message
bytes and re-frames whatever bytes it returns, so all this module does is
encode/decode these two messages.  Protobuf is not a dependency of BlackBull
(handlers own (de)serialisation), and for messages this small a hand-rolled
varint codec is clearer than pulling in ``protobuf`` — there are exactly two
``int32`` fields to read and one to write.

Wire check (from the HttpArena spec): the load generator sends the de-framed
payload ``08 01 10 02`` for ``SumRequest{a=1, b=2}``; ``GetSum`` must answer
``SumReply{result=3}`` → ``08 03``.
"""
from __future__ import annotations

from blackbull.grpc import GrpcServiceRegistry

_GETSUM_PATH = '/benchmark.BenchmarkService/GetSum'


def _read_varint(buf: bytes, i: int) -> tuple[int, int]:
    """Decode a base-128 varint from *buf* at *i*; return (value, next_index)."""
    result = 0
    shift = 0
    n = len(buf)
    while i < n:
        byte = buf[i]
        i += 1
        result |= (byte & 0x7F) << shift
        if not (byte & 0x80):
            break
        shift += 7
    return result, i


def _write_varint(value: int) -> bytes:
    """Encode *value* as a protobuf varint (int32 negatives use 64-bit two's
    complement, matching ``protoc``)."""
    if value < 0:
        value += 1 << 64
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            break
    return bytes(out)


def decode_sum_request(msg: bytes) -> tuple[int, int]:
    """Return ``(a, b)`` from a ``SumRequest`` body; missing fields default 0."""
    a = b = 0
    i = 0
    n = len(msg)
    while i < n:
        tag, i = _read_varint(msg, i)
        field, wire = tag >> 3, tag & 0x07
        if wire == 0:  # varint
            val, i = _read_varint(msg, i)
            if val >= (1 << 63):       # sign-extend an int32 negative
                val -= 1 << 64
            if field == 1:
                a = val
            elif field == 2:
                b = val
        elif wire == 2:  # length-delimited — skip defensively
            length, i = _read_varint(msg, i)
            i += length
        elif wire == 5:  # 32-bit
            i += 4
        elif wire == 1:  # 64-bit
            i += 8
        else:            # unknown/group — stop rather than loop
            break
    return a, b


def encode_sum_reply(result: int) -> bytes:
    """Encode ``SumReply{result}`` (field 1, varint)."""
    return b'\x08' + _write_varint(result)


async def get_sum(request: bytes, context) -> bytes:
    """Unary ``GetSum``: ``result = a + b``."""
    a, b = decode_sum_request(request)
    return encode_sum_reply(a + b)


def build_registry() -> GrpcServiceRegistry:
    """Registry wiring ``GetSum`` — pass to ``app.enable_grpc(...)``."""
    registry = GrpcServiceRegistry()
    registry.add_method(_GETSUM_PATH, get_sum)
    return registry
