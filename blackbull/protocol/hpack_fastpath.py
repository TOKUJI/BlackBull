"""HPACK static-index fast path.

Use only exact name/value matches. Static entries do not mutate the dynamic
table; literal encodings must still use the connection encoder.
"""
from itertools import chain

# RFC 7541 Appendix A — static-table entries with both name and value
# defined.  Keys are (name_bytes, value_bytes); values are the precomputed
# wire bytes for an Indexed Header Field (RFC 7541 §6.1).
_STATIC_INDEXED: dict[tuple[bytes, bytes], bytes] = {
    # Request-side pseudo-headers (used by PUSH_PROMISE on the server).
    (b':method', b'GET'):          bytes((0x80 | 2,)),
    (b':method', b'POST'):         bytes((0x80 | 3,)),
    (b':path',   b'/'):            bytes((0x80 | 4,)),
    (b':path',   b'/index.html'):  bytes((0x80 | 5,)),
    (b':scheme', b'http'):         bytes((0x80 | 6,)),
    (b':scheme', b'https'):        bytes((0x80 | 7,)),
    # Response-side pseudo-headers (used by HEADERS on the server).
    (b':status', b'200'):          bytes((0x80 | 8,)),
    (b':status', b'204'):          bytes((0x80 | 9,)),
    (b':status', b'206'):          bytes((0x80 | 10,)),
    (b':status', b'304'):          bytes((0x80 | 11,)),
    (b':status', b'400'):          bytes((0x80 | 12,)),
    (b':status', b'404'):          bytes((0x80 | 13,)),
    (b':status', b'500'):          bytes((0x80 | 14,)),
    # Defensive — accept-encoding is normally a request header, but
    # if a server ever emits it the static encoding still applies.
    (b'accept-encoding', b'gzip, deflate'): bytes((0x80 | 16,)),
}

_STATUS_INDEXED = {
    status: wire
    for (name, value), wire in _STATIC_INDEXED.items() if name == b':status'
    for status in (value, value.decode('ascii'))
}


def _coerce_bytes(v) -> bytes:
    if isinstance(v, bytes):
        return v
    if isinstance(v, str):
        return v.encode('ascii')
    return bytes(v)


def encode_status(encoder, status, fields) -> bytes:
    prefix = _STATUS_INDEXED.get(status)
    if prefix is None:
        return encoder.encode(chain(((b':status', status),), fields))
    if encoder.header_table.resized:
        return encode_with_prefix(encoder, prefix, fields)
    return prefix + encoder.encode(fields)


def pseudo_fast_bytes(name, value) -> bytes | None:
    """Return static-table encoding for a str/bytes name/value pair, or None.
    """
    return _STATIC_INDEXED.get((_coerce_bytes(name), _coerce_bytes(value)))


def encode_with_prefix(encoder, prefix: bytes, fields) -> bytes:
    """Emit pending table updates before a static-indexed field prefix."""
    if prefix and encoder.header_table.resized:
        prefix = encoder.encode(()) + prefix
    return prefix + encoder.encode(fields)
