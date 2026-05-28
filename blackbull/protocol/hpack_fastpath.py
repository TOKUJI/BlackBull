"""HPACK static-table fast path for outbound HEADERS / PUSH_PROMISE frames.

RFC 7541 Appendix A defines the HPACK static table.  When a header pair's
name AND value exactly match a static-table entry, the wire encoding is
a single byte: ``0x80 | index``.  This module precomputes those bytes
for every entry where both name and value are defined — primarily
``:status`` (every response) and the request-side pseudo-headers used
by PUSH_PROMISE (``:method``, ``:scheme``, ``:path``).

Wire equivalence: given the same input, the encoder produces identical
bytes.  Static-indexed fields do not mutate the encoder's dynamic table
(RFC 7541 §6.1), so emitting precomputed bytes outside the encoder
leaves dynamic-table state unchanged across requests.  Round-trip tests
in ``tests/conformance/http2/test_hpack_fastpath.py`` assert byte
equivalence.

Coverage of RFC 7541 Appendix A indices 1–61 with both name and value
defined (the only entries eligible for single-byte encoding):

  - 8–14   :status 200/204/206/304/400/404/500   (always-on response path)
  - 16     accept-encoding: gzip, deflate         (defensive — rare on responses)
  - 2–3    :method GET / POST                     (PUSH_PROMISE only)
  - 4–5    :path / and /index.html                (PUSH_PROMISE only)
  - 6–7    :scheme http / https                   (PUSH_PROMISE only)

All other static-table entries are name-only (e.g. ``content-type`` at
index 31 has no static value), so they cannot use the single-byte
indexed encoding.  Sprint 24 confirmed the table is exhausted at this
encoding tier — any further fastpath work would need to handle the
literal-with-indexed-name encoding (RFC 7541 §6.2), which involves
encoder choices (Huffman vs raw, incremental indexing vs not) that we
cannot pre-decide without coupling to encoder configuration.
"""

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


def _coerce_bytes(v) -> bytes:
    if isinstance(v, bytes):
        return v
    if isinstance(v, str):
        return v.encode('ascii')
    return bytes(v)


def status_fast_bytes(status_value) -> bytes | None:
    """Return the precomputed wire bytes for ``:status <value>`` when the
    value matches a static-table entry, else ``None``.

    Accepts ``str`` (the ASGI shape, e.g. ``'200'``) or ``bytes``.
    Kept as a separate function for backwards compatibility with the
    Sprint 21 Phase C ``Headers.save()`` call site.
    """
    return _STATIC_INDEXED.get((b':status', _coerce_bytes(status_value)))


def pseudo_fast_bytes(name, value) -> bytes | None:
    """Return the precomputed wire bytes for any static-indexed
    ``(name, value)`` pair, else ``None``.

    Used by :meth:`PushPromise.save` to fast-path the request-side
    pseudo-headers (``:method``, ``:scheme``, ``:path``).  ``name`` and
    ``value`` may be ``str`` (the ASGI shape) or ``bytes``.
    """
    return _STATIC_INDEXED.get((_coerce_bytes(name), _coerce_bytes(value)))
