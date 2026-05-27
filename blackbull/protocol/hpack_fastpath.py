"""HPACK static-table fast path for outbound HEADERS frames.

RFC 7541 Appendix A defines the HPACK static table.  When a header pair's
name AND value exactly match a static-table entry, the wire encoding is
a single byte: ``0x80 | index``.  This module precomputes those bytes
for the entries that arise as outbound server responses — primarily
``:status``, the dominant pseudo-header on every response — so the hot
path can skip ``hpack.Encoder.encode`` for the most-common header.

Wire equivalence: given the same input, the encoder produces identical
bytes.  Static-indexed fields do not mutate the encoder's dynamic table
(RFC 7541 §6.1), so emitting precomputed bytes outside the encoder
leaves dynamic-table state unchanged across requests.  A round-trip
test in ``tests/conformance/`` asserts byte equivalence.

Scope: server-response side only.  Request-side static entries
(``:method GET`` etc.) are out of scope here; the encoder's own
static-table lookup handles them on the rare paths that emit them
(PushPromise).
"""

# RFC 7541 Appendix A static table indices for entries with both name
# and value defined that show up on response paths:
#   8   :status 200
#   9   :status 204
#   10  :status 206
#   11  :status 304
#   12  :status 400
#   13  :status 404
#   14  :status 500
#   16  accept-encoding gzip, deflate
_STATIC_INDEXED: dict[tuple[bytes, bytes], bytes] = {
    (b':status', b'200'): bytes((0x80 | 8,)),
    (b':status', b'204'): bytes((0x80 | 9,)),
    (b':status', b'206'): bytes((0x80 | 10,)),
    (b':status', b'304'): bytes((0x80 | 11,)),
    (b':status', b'400'): bytes((0x80 | 12,)),
    (b':status', b'404'): bytes((0x80 | 13,)),
    (b':status', b'500'): bytes((0x80 | 14,)),
    (b'accept-encoding', b'gzip, deflate'): bytes((0x80 | 16,)),
}


def status_fast_bytes(status_value) -> bytes | None:
    """Return the precomputed wire bytes for ``:status <value>`` when the
    value matches a static-table entry, else ``None``.

    Accepts ``str`` (the ASGI shape, e.g. ``'200'``) or ``bytes``.
    """
    if isinstance(status_value, bytes):
        v = status_value
    elif isinstance(status_value, str):
        v = status_value.encode('ascii')
    else:
        v = bytes(status_value)
    return _STATIC_INDEXED.get((b':status', v))
