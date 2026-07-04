"""gRPC message compression (the ``grpc-encoding`` header + LPM Compressed-Flag).

Only ``gzip`` is implemented, via the stdlib :mod:`zlib` in gzip-framing mode
(RFC 1952); ``identity`` (no compression) needs no code.  gzip is the encoding
grpcio and grpc-go negotiate by default, so it covers the real interop cases.

Compression is a **transport** concern and lives here beside the LPM codec, not
in a middleware and not in a serialisation package: gRPC compresses each message
*inside* its Length-Prefixed-Message frame (the flag + length stay in clear), so
it is entangled with framing, not a whole-body byte transform a ``send``-wrapper
could apply.  A second codec (deflate, snappy, …) is the one anticipated
extension of *this* module: lift ``SUPPORTED`` to a
``{grpc-encoding-name: (compress, decompress)}`` registry and route it through
the two negotiation points already in :mod:`~blackbull.grpc.asgi` (the request
``grpc-encoding`` lookup and the response ``grpc-accept-encoding`` selection).
Deferred until a real second codec is needed — YAGNI, gzip is the interop floor.

Compression is only *one form* of gRPC runtime extension, and this registry is
**not** the runtime's extension model.  Do not fold other extension forms into
"compression": call-level concerns are gRPC interceptors (the analogue of
``@app.intercept``); service-level, protobuf-carrying features (reflection,
health, rich errors) are the Track B package; new wire protocols attach via the
framework Extension mechanism.  Keep the problem's scope where it belongs.

This module is pure compression, with **no** gRPC-status dependency (mirroring
:mod:`~blackbull.grpc.codec`): callers translate :class:`DecompressionError`
into the appropriate gRPC status.
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
    """Decompress a gzip *data* stream, refusing to produce more than
    *max_output* bytes.

    The gRPC 4-byte length prefix bounds only the *compressed* transfer size;
    a small compressed payload can inflate to gigabytes.  Decompression is
    therefore capped: :meth:`zlib.decompressobj.decompress` is given a
    ``max_length`` so it stops at the limit and parks any unprocessed input in
    ``unconsumed_tail`` — a non-empty tail (or an output past *max_output*)
    means the message would exceed the cap and raises
    :class:`DecompressionBombError`.  A corrupt stream (bad header or trailing
    CRC) raises :class:`DecompressionError` via the final :func:`flush`.
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
    if len(out) > max_output:
        raise DecompressionBombError(
            f'decompressed message exceeds the {max_output}-byte limit')
    return out
