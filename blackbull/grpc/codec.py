"""gRPC Length-Prefixed-Message codec.

The gRPC wire format (https://github.com/grpc/grpc/blob/master/doc/PROTOCOL-HTTP2.md)
carries each message inside an HTTP/2 DATA stream as:

    Length-Prefixed-Message = Compressed-Flag (1 byte, unsigned)
                              Message-Length  (4 bytes, big-endian uint32)
                              Message         (Message-Length bytes)

``Compressed-Flag`` is 0 for an uncompressed message and 1 when the message
body is compressed with the algorithm named in the ``grpc-encoding`` header.
This module is pure framing: it carries the flag but does not (de)compress —
:mod:`~blackbull.grpc.compression` holds the gzip codec and the ASGI bridge
sets/reads the flag around it.

This module is pure binary framing — no protobuf dependency.  Protobuf
serialisation is the application's concern; handlers receive and return the
raw message bytes.
"""
from __future__ import annotations

import struct

# 1-byte compressed flag + 4-byte big-endian length.
_PREFIX = struct.Struct('>BI')
_PREFIX_LEN = 5

# Defense-in-depth safety limit (16 MiB).  The 4-byte length prefix can
# declare up to 4 GiB; without a bound a forged prefix would make the decoder
# attempt a multi-gigabyte slice.  ``decode_messages`` rejects any declared
# length above this *before* touching the buffer.  The primary, configurable
# per-message limit lives at the gRPC layer (``serve_grpc``); this is the
# floor that protects the codec regardless of caller.
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
    DATA-frame boundaries).  Raises :class:`GrpcDecodeError` on a truncated
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
