"""Wire-equivalence tests for the Sprint 21 Phase C HPACK fast path.

The fast path in ``blackbull/protocol/hpack_fastpath.py`` precomputes the
single-byte ``Indexed Header Field`` encoding for RFC 7541 Appendix A
entries (notably ``:status 200``).  These tests assert:

1. Bytes produced by ``Headers.save()`` decode back to the original
   ``(name, value)`` pairs.
2. Bytes produced by ``Headers.save()`` are identical to what
   ``hpack.Encoder.encode`` would emit on its own (the dynamic-table
   neutrality claim of RFC 7541 §6.1).
3. Sequential ``Headers.save()`` calls sharing one Encoder stay
   wire-equivalent across multiple requests (dynamic-table state
   evolves identically).
"""
from hpack import Decoder, Encoder
import pytest

from blackbull.protocol.frame_types import (
    FrameTypes,
    HeaderFrameFlags,
    Headers,
    PseudoHeaders,
)
from blackbull.protocol import hpack_fastpath


def _make_headers(encoder, status: str, headers: list[tuple[bytes, bytes]]) -> Headers:
    """Build an outbound Headers frame with the given :status + regulars."""
    h = Headers(
        length=0,
        type_=FrameTypes.HEADERS.value,
        flags=HeaderFrameFlags.END_HEADERS,
        stream_id=1,
        encoder=encoder,
    )
    h.pseudo_headers[PseudoHeaders.STATUS] = status
    h.headers.extend(headers)
    return h


def _extract_payload(frame_bytes: bytes) -> bytes:
    """Strip the 9-byte frame header to return just the HPACK payload."""
    return frame_bytes[9:]


@pytest.mark.parametrize("status", ["200", "204", "206", "304", "400", "404", "500"])
def test_status_decodes_round_trip(status: str):
    """save() output decodes back to the original :status + headers."""
    encoder = Encoder()
    decoder = Decoder()
    h = _make_headers(encoder, status, [
        (b"content-type", b"text/plain"),
        (b"content-length", b"4"),
    ])
    payload = _extract_payload(h.save())
    fields = decoder.decode(payload, raw=True)
    fields_b = [(bytes(k), bytes(v)) for k, v in fields]
    assert (b":status", status.encode()) in fields_b
    assert (b"content-type", b"text/plain") in fields_b
    assert (b"content-length", b"4") in fields_b


@pytest.mark.parametrize("status", ["200", "204", "206", "304", "400", "404", "500"])
def test_status_wire_equivalence_with_encoder_only(status: str):
    """Fast-path output must equal what the encoder would have emitted alone.

    Two encoders constructed in parallel — one fed only the residual
    (without :status), one fed the full set — must produce wire bytes
    that decode to the same header list.  The fast-path output prepends
    the precomputed static-indexed byte to the residual encoder output;
    full-encoder output emits its own static-indexed byte for :status.
    Both decoders therefore see byte-identical streams.
    """
    enc_fast = Encoder()
    enc_full = Encoder()
    h_fast = _make_headers(enc_fast, status, [
        (b"content-type", b"text/plain"),
        (b"content-length", b"4"),
    ])
    fast_payload = _extract_payload(h_fast.save())

    full_payload = enc_full.encode([
        (b":status", status.encode()),
        (b"content-type", b"text/plain"),
        (b"content-length", b"4"),
    ])

    assert fast_payload == full_payload, (
        f"fast-path output diverges from encoder-only on :status {status} "
        f"— fast={fast_payload!r}, full={full_payload!r}"
    )


def test_unlisted_status_falls_through_to_encoder():
    """A status not in the static-table fast-path map (e.g. 201) must
    still encode correctly via the encoder."""
    assert hpack_fastpath.status_fast_bytes("201") is None
    encoder = Encoder()
    h = _make_headers(encoder, "201", [(b"content-length", b"0")])
    payload = _extract_payload(h.save())

    decoder = Decoder()
    fields = decoder.decode(payload, raw=True)
    fields_b = [(bytes(k), bytes(v)) for k, v in fields]
    assert (b":status", b"201") in fields_b


def test_sequential_calls_share_dynamic_table_state():
    """The dynamic table evolves identically whether :status flows through
    the encoder or the fast path.  Concretely: with one shared encoder,
    five back-to-back Headers.save() calls must produce a byte stream
    that decodes to the same header sequences as five back-to-back
    encoder-only calls."""
    enc_fast = Encoder()
    enc_full = Encoder()
    dec_fast = Decoder()
    dec_full = Decoder()

    for i in range(5):
        regulars = [
            (b"content-type", b"text/plain"),
            (b"content-length", str(i).encode()),
            (b"x-request-id", f"req-{i}".encode()),
        ]
        # Fast path via Headers.save
        h = _make_headers(enc_fast, "200", regulars)
        fast_payload = _extract_payload(h.save())
        # Full path via raw encoder
        full_payload = enc_full.encode(
            [(b":status", b"200")] + regulars
        )
        assert fast_payload == full_payload, (
            f"iteration {i}: fast/full diverged; "
            f"fast={fast_payload!r}, full={full_payload!r}"
        )
        # Both decoders should produce the same header list when fed each stream
        fast_fields = [(bytes(k), bytes(v)) for k, v in dec_fast.decode(fast_payload, raw=True)]
        full_fields = [(bytes(k), bytes(v)) for k, v in dec_full.decode(full_payload, raw=True)]
        assert fast_fields == full_fields


def test_status_only_response_is_single_byte():
    """A response with no regular headers and :status 200 should encode
    to exactly one byte (0x88) — confirms the fast-path bypasses any
    per-call encoder overhead in the most-degenerate case."""
    encoder = Encoder()
    h = _make_headers(encoder, "200", [])
    payload = _extract_payload(h.save())
    assert payload == bytes((0x88,))


# ---------------------------------------------------------------------------
# Sprint 24 — request-side fastpath (PUSH_PROMISE pseudo-headers)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("name,value,expected_byte", [
    (b":method", b"GET",         0x80 | 2),
    (b":method", b"POST",        0x80 | 3),
    (b":path",   b"/",           0x80 | 4),
    (b":path",   b"/index.html", 0x80 | 5),
    (b":scheme", b"http",        0x80 | 6),
    (b":scheme", b"https",       0x80 | 7),
])
def test_pseudo_fast_bytes_static_entries(name: bytes, value: bytes, expected_byte: int):
    """Sprint 24 — every static-table entry with both name and value
    defined returns the expected single-byte Indexed Header Field."""
    wire = hpack_fastpath.pseudo_fast_bytes(name, value)
    assert wire == bytes((expected_byte,))


@pytest.mark.parametrize("name,value", [
    (b":method", b"PATCH"),       # PATCH is not in the static table
    (b":path",   b"/admin"),      # arbitrary paths fall through
    (b":scheme", b"ftp"),         # ftp not in the static table
    (b":authority", b"example.com"),  # :authority (idx 1) has no static value
])
def test_pseudo_fast_bytes_misses(name: bytes, value: bytes):
    """Pairs not in the static table return None — caller must fall
    back to the encoder."""
    assert hpack_fastpath.pseudo_fast_bytes(name, value) is None


def test_push_promise_save_uses_fastpath():
    """PushPromise.save() must prepend the static-indexed bytes for
    matched pseudo-headers and produce the same wire output as the
    encoder alone."""
    from blackbull.protocol.frame_types import PushPromise

    enc_fast = Encoder()
    enc_full = Encoder()

    pp = PushPromise(
        length=0,
        type_=FrameTypes.PUSH_PROMISE.value,
        flags=0,
        stream_id=1,
        data=(2).to_bytes(4, 'big'),
        encoder=enc_fast,
    )
    pp.pseudo_headers[PseudoHeaders.METHOD] = "GET"
    pp.pseudo_headers[PseudoHeaders.SCHEME] = "https"
    pp.pseudo_headers[PseudoHeaders.PATH]   = "/"
    pp.pseudo_headers[PseudoHeaders.AUTHORITY] = "example.com"
    pp.headers.append((b"cache-control", b"max-age=60"))

    fast_bytes = pp.save()
    # Strip the 9-byte frame header + 4-byte promised_stream_id prefix.
    fast_payload = fast_bytes[9 + 4:]

    full_payload = enc_full.encode([
        (b":method", b"GET"),
        (b":scheme", b"https"),
        (b":path", b"/"),
        (b":authority", b"example.com"),
        (b"cache-control", b"max-age=60"),
    ])

    assert fast_payload == full_payload, (
        f"PushPromise fastpath diverged from encoder-only output; "
        f"fast={fast_payload!r}, full={full_payload!r}"
    )

    # And the decoded fields must match the inputs.
    dec = Decoder()
    fields = [(bytes(k), bytes(v)) for k, v in dec.decode(fast_payload, raw=True)]
    assert (b":method", b"GET") in fields
    assert (b":scheme", b"https") in fields
    assert (b":path", b"/") in fields
    assert (b":authority", b"example.com") in fields
    assert (b"cache-control", b"max-age=60") in fields
