"""Per-message gRPC gzip compression.

The flag and length stay uncompressed, so this is not whole-body middleware.
Callers translate DecompressionError into status; this codec owns no status.
"""
from __future__ import annotations

import zlib

# wbits = 16 + MAX_WBITS selects the gzip header/trailer (RFC 1952) with the
# largest window; both compressobj and decompressobj take it to mean *gzip*
# framing rather than the raw-deflate or zlib-wrapped formats.
_GZIP_WBITS = 16 + zlib.MAX_WBITS

# Advertised in ``grpc-accept-encoding`` (what the server can decode) and used
# for response compression.  ``identity`` is always acceptable.
ACCEPT_ENCODING = b'identity,gzip'

# Message-level ``grpc-encoding`` values this module can (de)compress; the
# no-op ``identity``/empty case is handled by the caller (no coding applied).
SUPPORTED = frozenset({b'gzip'})


def supports(encoding: bytes) -> bool:
    """Return ``True`` if *encoding* is one this module can (de)compress."""
    return encoding in SUPPORTED


class DecompressionError(ValueError):
    """A compressed request message could not be decompressed (corrupt
    stream, or an output larger than the caller's limit)."""


class DecompressionBombError(DecompressionError):
    """Decompressed output exceeded the caller's size limit — a small
    compressed frame that inflates past the per-message cap (a "zip bomb")."""


def compress_gzip(data: bytes) -> bytes:
    """Compress *data* into a single gzip stream."""
    c = zlib.compressobj(wbits=_GZIP_WBITS)
    return c.compress(data) + c.flush()


def decompress_gzip(data: bytes, max_output: int) -> bytes:
    """Decompress one gzip member, refusing to produce more than *max_output*.

    The 4-byte LPM prefix bounds the *compressed* size only, so the cap uses
    ``decompress(..., max_output + 1)`` and ``unconsumed_tail`` and raises
    [`DecompressionBombError`][].

    A message is exactly one complete member, so input that ends before the
    member does (its CRC was never verified; mid-deflate the output is silently
    partial), a stream ``zlib`` rejects and any trailing byte — a second member
    included — raise [`DecompressionError`][].  That includes an empty body.
    """
    d = zlib.decompressobj(wbits=_GZIP_WBITS)
    try:
        out = d.decompress(data, max_output + 1)
    except zlib.error as exc:
        raise DecompressionError(f'gzip: {exc}') from exc
    if d.unconsumed_tail or len(out) > max_output:
        raise DecompressionBombError(
            f'decompressed message exceeds the {max_output}-byte limit')
    try:
        out += d.flush()
    except zlib.error as exc:
        raise DecompressionError(f'gzip: {exc}') from exc
    if not d.eof:
        raise DecompressionError('gzip: the stream ends before the member does')
    if d.unused_data:
        raise DecompressionError(
            f'gzip: {len(d.unused_data)} byte(s) after the member')
    if len(out) > max_output:
        raise DecompressionBombError(
            f'decompressed message exceeds the {max_output}-byte limit')
    return out
