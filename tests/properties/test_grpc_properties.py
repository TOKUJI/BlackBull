"""Hypothesis property-based tests for the gRPC codec and ASGI bridge.

Uses the ``@given`` decorator from Hypothesis to generate random inputs
and verify invariants that hold for ALL valid inputs, not just hand-picked
examples.

Properties tested:

* **codec round-trip**: ``decode(encode(payload)) == [(False, payload)]``
* **decode never crashes**: ``decode_messages(data)`` raises at most ``GrpcDecodeError``
* **encode prefix consistency**: length prefix matches ``len(payload)``
* **multi-message round-trip**: concatenated encodes decode correctly
* **percent-encode idempotence**: ``_pct_encode`` is pure ASCII output
* **GrpcStatus value range**: all status codes are 0..16
* **GrpcError invariants**: status and details are preserved
"""
from __future__ import annotations

import struct

import pytest
from hypothesis import assume, given, strategies as st

from blackbull.grpc.codec import encode_message, decode_messages, GrpcDecodeError
from blackbull.grpc.asgi import _pct_encode_message
from blackbull.grpc.status import GrpcStatus, GrpcError


# ---------------------------------------------------------------------------
# §1 — Codec round-trip properties
# ---------------------------------------------------------------------------

class TestCodecRoundTrip:
    """``decode(encode(p)) == [(False, p)]`` for all byte sequences."""

    @given(payload=st.binary(max_size=1024 * 64))
    def test_single_message_round_trip(self, payload):
        """encode → decode must be identity for any payload up to 64 KiB."""
        framed = encode_message(payload)
        result = decode_messages(framed)
        assert result == [(False, payload)]

    @given(payload=st.binary(max_size=1024))
    def test_compressed_round_trip_preserves_flag(self, payload):
        """Compressed flag must round-trip correctly."""
        framed = encode_message(payload, compressed=True)
        result = decode_messages(framed)
        assert result == [(True, payload)]

    @given(payloads=st.lists(st.binary(min_size=0, max_size=256),
                             min_size=0, max_size=20))
    def test_multi_message_round_trip(self, payloads):
        """Concatenated encoded messages must decode to the original list."""
        buf = b''.join(encode_message(p) for p in payloads)
        result = decode_messages(buf)
        assert result == [(False, p) for p in payloads]

    @given(payloads=st.lists(st.binary(min_size=0, max_size=256),
                             min_size=1, max_size=10))
    def test_message_boundaries_are_preserved(self, payloads):
        """Each decoded message must have exactly the right boundaries,
        even when adjacent messages have different lengths."""
        buf = b''.join(encode_message(p) for p in payloads)
        result = decode_messages(buf)
        assert len(result) == len(payloads)
        for (compressed, decoded), original in zip(result, payloads):
            assert compressed is False
            assert decoded == original


# ---------------------------------------------------------------------------
# §2 — Encode invariants
# ---------------------------------------------------------------------------

class TestEncodeInvariants:
    """Structural properties of the encoded Length-Prefixed-Message."""

    @given(payload=st.binary(max_size=1024 * 64))
    def test_encoded_length_is_payload_plus_five(self, payload):
        """An encoded message must be exactly ``len(payload) + 5`` bytes."""
        framed = encode_message(payload)
        assert len(framed) == len(payload) + 5

    @given(payload=st.binary(max_size=1024 * 64))
    def test_prefix_length_matches_payload(self, payload):
        """The 4-byte big-endian length prefix must equal ``len(payload)``."""
        framed = encode_message(payload)
        declared_len = int.from_bytes(framed[1:5], 'big')
        assert declared_len == len(payload)

    @given(payload=st.binary(max_size=1024 * 64))
    def test_compressed_flag_is_zero_or_one(self, payload):
        """The first byte of the frame must be 0 or 1."""
        framed_uncompressed = encode_message(payload, compressed=False)
        framed_compressed = encode_message(payload, compressed=True)
        assert framed_uncompressed[0] == 0
        assert framed_compressed[0] == 1

    @given(payload=st.binary(max_size=1024 * 64))
    def test_payload_appears_after_five_byte_prefix(self, payload):
        """The payload bytes must appear verbatim at offset 5."""
        framed = encode_message(payload)
        assert framed[5:] == payload


# ---------------------------------------------------------------------------
# §3 — Decode safety properties
# ---------------------------------------------------------------------------

class TestDecodeSafety:
    """``decode_messages`` must never crash on arbitrary input — only
    raise ``GrpcDecodeError`` for malformed data."""

    @given(data=st.binary(max_size=1024))
    def test_decode_never_raises_unexpected_exception(self, data):
        """Any byte sequence up to 1 KiB must either decode successfully
        or raise ``GrpcDecodeError``.  No other exception type."""
        try:
            result = decode_messages(data)
            # If successful, verify the result structure
            assert isinstance(result, list)
            for item in result:
                assert isinstance(item, tuple)
                assert len(item) == 2
                assert isinstance(item[0], bool)
                assert isinstance(item[1], bytes)
        except GrpcDecodeError:
            pass  # expected for malformed data
        except Exception as exc:
            raise AssertionError(
                f'decode_messages raised unexpected {type(exc).__name__}: '
                f'{exc} for input {data!r}') from exc

    @given(data=st.binary(min_size=0, max_size=256))
    def test_decode_empty_or_small_never_crashes(self, data):
        """Small inputs (0–256 bytes) must never crash."""
        try:
            decode_messages(data)
        except GrpcDecodeError:
            pass

    @given(payloads=st.lists(st.binary(min_size=0, max_size=128),
                             min_size=0, max_size=30))
    def test_decode_valid_sequence_always_succeeds(self, payloads):
        """Any sequence of validly encoded messages must decode without error."""
        buf = b''.join(encode_message(p) for p in payloads)
        result = decode_messages(buf)
        assert isinstance(result, list)
        assert len(result) == len(payloads)


# ---------------------------------------------------------------------------
# §4 — Percent-encoding properties
# ---------------------------------------------------------------------------

class TestPercentEncoding:
    """``_pct_encode_message`` invariants."""

    @given(text=st.text(min_size=0, max_size=256))
    def test_output_is_always_ascii(self, text):
        """Percent-encoded output must contain only ASCII bytes."""
        result = _pct_encode_message(text)
        try:
            result.decode('ascii')
        except UnicodeDecodeError:
            raise AssertionError(
                f'percent-encoded output is not pure ASCII: {result!r}')

    @given(text=st.text(min_size=0, max_size=256))
    def test_output_contains_no_control_characters(self, text):
        """Percent-encoded output must contain no raw bytes < 0x20
        (except for 0x20 = space which is allowed)."""
        result = _pct_encode_message(text)
        for b in result:
            if b < 0x20:
                raise AssertionError(
                    f'percent-encoded output contains control byte 0x{b:02X}: '
                    f'{result!r}')

    @given(text=st.text(min_size=1, max_size=128))
    def test_no_raw_percent_sign_in_output(self, text):
        """Every '%' in the input must become '%25' in the output."""
        result = _pct_encode_message(text)
        # Count % signs: each input % → %25 (3 bytes), so we can't
        # just count occurrences.  Instead verify no % in output is
        # followed by anything other than two hex digits.
        i = 0
        while i < len(result):
            if result[i:i + 1] == b'%':
                assert i + 2 < len(result), (
                    f'truncated percent sequence in {result!r}')
                hex_part = result[i + 1:i + 3]
                assert hex_part.isalnum() or all(
                    c in b'0123456789ABCDEF' for c in hex_part.upper()), (
                    f'invalid percent sequence %{hex_part.decode()} in {result!r}')
                i += 3
            else:
                i += 1

    @given(text=st.text(min_size=0, max_size=128))
    def test_printable_ascii_passes_through(self, text):
        """Characters 0x20–0x7E (except '%') must appear verbatim
        in the output.  We verify by re-decoding: a percent-encoded
        ASCII-only string must round-trip to the original."""
        # For ASCII-only input (minus '%'), encoding must be identity.
        ascii_text = ''.join(
            c for c in text
            if '\x20' <= c <= '\x7e' and c != '%'
        )
        result = _pct_encode_message(ascii_text)
        # All chars are in the 0x20-0x7E range and not '%', so no encoding needed
        assert result == ascii_text.encode('ascii'), (
            f'ASCII text {ascii_text!r} was modified: got {result!r}')

    @given(text=st.text(min_size=0, max_size=128))
    def test_empty_string_yields_empty_bytes(self, text):
        """If the input is empty, output must be empty."""
        result = _pct_encode_message(text)
        assert (len(result) == 0) == (len(text) == 0)


# ---------------------------------------------------------------------------
# §5 — GrpcStatus and GrpcError properties
# ---------------------------------------------------------------------------

class TestGrpcStatusProperties:
    """Invariants of ``GrpcStatus`` and ``GrpcError``."""

    def test_all_status_codes_are_in_range_0_to_16(self):
        """Every ``GrpcStatus`` member must have value 0 ≤ n ≤ 16."""
        for status in GrpcStatus:
            assert 0 <= int(status) <= 16

    def test_grpc_error_preserves_status(self):
        """``GrpcError.status`` must equal the input status."""
        for code in GrpcStatus:
            err = GrpcError(code, 'test')
            assert err.status == code
            assert err.details == 'test'

    @given(code=st.sampled_from(list(GrpcStatus)),
           details=st.text(min_size=0, max_size=64))
    def test_grpc_error_str_contains_status_name(self, code, details):
        """``str(GrpcError(code, details))`` must contain the status name."""
        err = GrpcError(code, details)
        assert code.name in str(err)

    def test_grpc_status_enum_has_exactly_17_members(self):
        """The canonical gRPC status set has exactly 17 codes (0–16)."""
        assert len(GrpcStatus) == 17
