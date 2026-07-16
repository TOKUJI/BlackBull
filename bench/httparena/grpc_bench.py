"""HttpArena gRPC profiles — ``benchmark.BenchmarkService`` (``GetSum`` +
``StreamSum``).

The proto is tiny::

    service BenchmarkService {
        rpc GetSum    (SumRequest)    returns (SumReply);
        rpc StreamSum (StreamRequest) returns (stream SumReply);
    }
    message SumRequest    { int32 a = 1; int32 b = 2; }
    message StreamRequest { int32 a = 1; int32 b = 2; int32 count = 3; }
    message SumReply      { int32 result = 1; }

BlackBull's gRPC bridge hands the handler the *de-framed* request message
bytes and re-frames whatever bytes it returns (for a server-streaming handler,
each ``yield`` becomes one gRPC DATA frame), so all this module does is
encode/decode these messages.  Protobuf is not a dependency of BlackBull
(handlers own (de)serialisation), and for messages this small a hand-rolled
varint codec is clearer than pulling in ``protobuf`` — there are a handful of
``int32`` fields to read and one to write.

Wire checks (from the HttpArena spec):

- ``unary-grpc``: the load generator sends the de-framed payload
  ``08 01 10 02`` for ``SumRequest{a=1, b=2}``; ``GetSum`` answers
  ``SumReply{result=3}`` → ``08 03``.
- ``stream-grpc``: ``StreamSum(StreamRequest{a=13, b=42, count=N})`` must emit
  exactly ``N`` ``SumReply`` messages, the *i*-th carrying
  ``result = 13 + 42 + i`` for ``i`` in ``0..N-1``.  ``count=0`` emits **zero**
  messages (anti-cheat check), so ``count`` is *not* clamped up to 1 — the
  profile's "clamp count >= 1" note contradicts its own ``count: 0`` validation,
  and the validation is what actually runs; only negatives are floored to 0.
"""
from __future__ import annotations

from blackbull.grpc import GrpcServiceRegistry

_GETSUM_PATH = '/benchmark.BenchmarkService/GetSum'
_STREAMSUM_PATH = '/benchmark.BenchmarkService/StreamSum'


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


def decode_stream_request(msg: bytes) -> tuple[int, int, int]:
    """Return ``(a, b, count)`` from a ``StreamRequest`` body (fields 1/2/3,
    all varint ``int32``); missing fields default 0."""
    a = b = count = 0
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
            elif field == 3:
                count = val
        elif wire == 2:  # length-delimited — skip defensively
            length, i = _read_varint(msg, i)
            i += length
        elif wire == 5:  # 32-bit
            i += 4
        elif wire == 1:  # 64-bit
            i += 8
        else:            # unknown/group — stop rather than loop
            break
    return a, b, count


def encode_sum_reply(result: int) -> bytes:
    """Encode ``SumReply{result}`` (field 1, varint)."""
    return b'\x08' + _write_varint(result)


async def get_sum(request: bytes, context) -> bytes:
    """Unary ``GetSum``: ``result = a + b``."""
    a, b = decode_sum_request(request)
    return encode_sum_reply(a + b)


async def stream_sum(request: bytes, context):
    """Server-streaming ``StreamSum``: emit ``count`` ``SumReply`` messages,
    the *i*-th carrying ``result = a + b + i``.

    Async-generator handler — each ``yield`` is re-framed by the gRPC bridge
    into one DATA frame.  ``count`` is emitted exactly (``count=0`` → zero
    messages, per the profile's anti-cheat validation); only negative counts
    are floored to 0 so a stray negative can't loop or crash."""
    a, b, count = decode_stream_request(request)
    base = a + b
    for i in range(count if count > 0 else 0):
        yield encode_sum_reply(base + i)


def build_registry() -> GrpcServiceRegistry:
    """Registry wiring ``GetSum`` (unary) + ``StreamSum`` (server-streaming) —
    pass to ``app.enable_grpc(...)``.  ``StreamSum``'s streaming-ness is
    auto-detected from its async-generator signature."""
    registry = GrpcServiceRegistry()
    registry.add_method(_GETSUM_PATH, get_sum)
    registry.add_method(_STREAMSUM_PATH, stream_sum)
    return registry
