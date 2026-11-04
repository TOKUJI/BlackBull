"""HttpArena's unary gRPC profiles: ``benchmark.BenchmarkService/GetSum``.

``SumRequest{int32 a = 1; int32 b = 2}`` answers ``SumReply{int32 result = 1}``
with ``a + b``.  The bridge hands the handler the de-framed message, so this
module only reads and writes the two protobuf varint fields.
"""
from __future__ import annotations

from blackbull.grpc import GrpcServiceRegistry


def _read_varint(buf: bytes, i: int) -> tuple[int, int]:
    result = shift = 0
    while i < len(buf):
        byte = buf[i]
        i += 1
        result |= (byte & 0x7F) << shift
        if not byte & 0x80:
            break
        shift += 7
    return result, i


def _write_varint(value: int) -> bytes:
    if value < 0:                      # int32 negatives: 64-bit two's complement
        value += 1 << 64
    out = bytearray()
    while True:
        byte, value = value & 0x7F, value >> 7
        out.append(byte | 0x80 if value else byte)
        if not value:
            return bytes(out)


def decode_sum_request(msg: bytes) -> tuple[int, int]:
    fields = {1: 0, 2: 0}
    i = 0
    while i < len(msg):
        tag, i = _read_varint(msg, i)
        wire = tag & 0x07
        if wire == 0:
            value, i = _read_varint(msg, i)
            fields[tag >> 3] = value - (1 << 64) if value >= 1 << 63 else value
        elif wire == 2:
            length, i = _read_varint(msg, i)
            i += length
        elif wire in (1, 5):
            i += 8 if wire == 1 else 4
        else:
            break
    return fields[1], fields[2]


async def get_sum(request: bytes, context) -> bytes:
    a, b = decode_sum_request(request)
    return b'\x08' + _write_varint(a + b)


def build_registry() -> GrpcServiceRegistry:
    registry = GrpcServiceRegistry()
    registry.add_method('/benchmark.BenchmarkService/GetSum', get_sum)
    return registry
