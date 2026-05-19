"""WebSocket frame codec (RFC 6455).

Shared by the server-side sender/recipient and the client.  Protocol-specific
callers (WebSocketSender, WebSocketRecipient, WebSocketSession) import the
module-level functions directly.
"""
import os
from enum import IntEnum
from typing import NamedTuple


class WSOpcode(IntEnum):
    CONTINUATION = 0x0
    TEXT         = 0x1
    BINARY       = 0x2
    CLOSE        = 0x8
    PING         = 0x9
    PONG         = 0xA


class WSFrameBits(IntEnum):
    FIN         = 0x80  # FIN bit in byte 0
    RSV1        = 0x40  # RSV1 bit in byte 0 (per-message deflate, RFC 7692)
    RSV2        = 0x20  # RSV2 bit in byte 0 (reserved)
    RSV3        = 0x10  # RSV3 bit in byte 0 (reserved)
    OPCODE_MASK = 0x0F  # opcode bits in byte 0
    MASK_BIT    = 0x80  # mask bit in byte 1
    LENGTH_MASK = 0x7F  # payload length bits in byte 1


class WSFrameHeader(NamedTuple):
    """Decoded fields from the two-byte WebSocket frame header (RFC 6455 §5.2).

    Note on masking (RFC 6455 §5.1):
    - Client → server frames MUST be masked;
      ``WebSocketRecipient`` raises ``ValueError`` on an unmasked client frame.
    - Server → client frames MUST NOT be masked; ``WebSocketSender`` never sets
      the mask bit.
    """
    opcode: int
    masked: bool
    length: int
    fin:    bool
    rsv1:   bool
    rsv2:   bool
    rsv3:   bool


def encode_frame(payload: bytes, opcode: WSOpcode | int = WSOpcode.TEXT,
                 *, mask: bool = False, rsv1: bool = False) -> bytes:
    """Encode *payload* as a WebSocket data frame (RFC 6455 §5).

    ``opcode`` defaults to ``WSOpcode.TEXT``; pass ``WSOpcode.BINARY`` for
    binary frames, ``WSOpcode.CLOSE`` for close frames, etc.

    Masking (RFC 6455 §5.1):
    - Server → client frames MUST NOT be masked: keep ``mask=False`` (default).
    - Client → server frames MUST be masked: pass ``mask=True``, which prepends
      a random 4-byte masking key and XORs the payload with it.

    RSV1 (RFC 7692 §7): pass ``rsv1=True`` on the FIRST frame of a message
    whose payload has been compressed with permessage-deflate.  Continuation
    frames in the same message keep ``rsv1=False``.
    """
    length = len(payload)
    first_byte = WSFrameBits.FIN | opcode
    if rsv1:
        first_byte |= WSFrameBits.RSV1
    header = bytes([first_byte])
    mask_bit = WSFrameBits.MASK_BIT if mask else 0
    if length < 126:
        header += bytes([mask_bit | length])
    elif length < 65536:
        header += bytes([mask_bit | 126]) + length.to_bytes(2, 'big')
    else:
        header += bytes([mask_bit | 127]) + length.to_bytes(8, 'big')
    if mask:
        mask_key = os.urandom(4)
        masked_payload = bytes(b ^ mask_key[i % 4] for i, b in enumerate(payload))
        return header + mask_key + masked_payload
    return header + payload


async def read_frame_header(reader) -> WSFrameHeader:
    """Read the two-byte WebSocket frame header (RFC 6455 §5.2).

    Returns a ``WSFrameHeader`` with all decoded flag and length fields.
    RSV1 signals per-message deflate (RFC 7692 §7); RSV2 and RSV3 are
    reserved for future extensions.
    Raises ``asyncio.IncompleteReadError`` on EOF.
    """
    header = await reader.readexactly(2)
    return WSFrameHeader(
        opcode = header[0] & WSFrameBits.OPCODE_MASK,
        masked = bool(header[1] & WSFrameBits.MASK_BIT),
        length = header[1] & WSFrameBits.LENGTH_MASK,
        fin    = bool(header[0] & WSFrameBits.FIN),
        rsv1   = bool(header[0] & WSFrameBits.RSV1),
        rsv2   = bool(header[0] & WSFrameBits.RSV2),
        rsv3   = bool(header[0] & WSFrameBits.RSV3),
    )


async def read_payload(reader, masked: bool, length: int) -> bytes:
    """Read the payload of a WebSocket frame from *reader*.

    If *masked* is True, also read the 4-byte mask and unmask the payload.
    Raises ``asyncio.IncompleteReadError`` on EOF.
    """
    if length == 126:
        length = int.from_bytes(await reader.readexactly(2), 'big')
    elif length == 127:
        length = int.from_bytes(await reader.readexactly(8), 'big')

    if not masked:
        return await reader.readexactly(length)

    mask = await reader.readexactly(4)
    raw = await reader.readexactly(length)
    if length == 0:
        return raw
    # XOR via int arithmetic — does the work at C speed via CPython's
    # bignum routines.  Equivalent to ``bytes(b ^ mask[i % 4] ...)`` but
    # ~20× faster on real payload sizes; with Autobahn 12/13 fragmenting
    # large compressed messages into hundreds of small frames, the slow
    # comprehension dominated the read loop.
    extended_mask = mask * ((length + 3) // 4)
    if len(extended_mask) > length:
        extended_mask = extended_mask[:length]
    return (int.from_bytes(raw, 'big')
            ^ int.from_bytes(extended_mask, 'big')
            ).to_bytes(length, 'big')


async def read_frame(reader) -> tuple[int, bytes]:
    """Read one WebSocket frame from *reader*.

    Returns ``(opcode, payload)`` where *payload* is already unmasked.
    Raises ``asyncio.IncompleteReadError`` on EOF.
    """
    h = await read_frame_header(reader)
    payload = await read_payload(reader, h.masked, h.length)
    return h.opcode, payload
