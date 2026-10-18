"""gRPC length-prefixed message framing.

The compressed flag travels here but compression belongs to the compression
module. Protobuf serialization remains the handler's responsibility.
"""
from __future__ import annotations

import struct

# 1-byte compressed flag + 4-byte big-endian length.
_PREFIX = struct.Struct('>BI')
_PREFIX_LEN = _PREFIX.size

# Encoded-message ceiling, separate from the configurable decoded-message cap.
MAX_MESSAGE_LENGTH = 16 * 1024 * 1024


class GrpcDecodeError(ValueError):
    """Raised when a DATA buffer is not a valid sequence of
    Length-Prefixed-Messages (truncated prefix or short body)."""


def encode_message(payload: bytes, *, compressed: bool = False) -> bytes:
    """Frame *payload* as a single gRPC Length-Prefixed-Message."""
    return _PREFIX.pack(1 if compressed else 0, len(payload)) + payload


def decode_messages(data: bytes) -> list[tuple[bool, bytes]]:
    """Parse *data* into a list of ``(compressed, payload)`` messages.

    A single DATA buffer may contain zero, one, or many framed messages
    (gRPC permits multiple messages per stream and does not align them to
    DATA-frame boundaries).  Raises [`GrpcDecodeError`][] on a truncated
    prefix or a message body shorter than its declared length.
    """
    messages: list[tuple[bool, bytes]] = []
    offset = 0
    n = len(data)
    while offset < n:
        if n - offset < _PREFIX_LEN:
            raise GrpcDecodeError(
                f'truncated message prefix: {n - offset} byte(s) before EOF')
        flag, length = _PREFIX.unpack_from(data, offset)
        offset += _PREFIX_LEN
        if length > MAX_MESSAGE_LENGTH:
            raise GrpcDecodeError(
                f'message length {length} exceeds safety limit '
                f'{MAX_MESSAGE_LENGTH}')
        end = offset + length
        if end > n:
            raise GrpcDecodeError(
                f'message body truncated: need {length} bytes, have {n - offset}')
        messages.append((bool(flag), data[offset:end]))
        offset = end
    return messages
