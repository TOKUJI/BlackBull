"""gRPC Length-Prefixed-Message codec conformance tests.

Validates the gRPC wire format codec against the specification at
https://github.com/grpc/grpc/blob/master/doc/PROTOCOL-HTTP2.md .

The gRPC message framing is:

    Length-Prefixed-Message = Compressed-Flag (1 byte, unsigned)
                              Message-Length  (4 bytes, big-endian uint32)
                              Message         (Message-Length bytes)

These tests verify encoding/decoding correctness and error handling
without spinning up a live server — pure byte-level assertions.
"""
from __future__ import annotations

import struct

import pytest

from blackbull.grpc.codec import encode_message, decode_messages, GrpcDecodeError


# ---------------------------------------------------------------------------
# §1 — Encoding correctness (RFC: wire format)
# ---------------------------------------------------------------------------

class TestEncodeWireFormat:
    """Verify that ``encode_message`` produces the exact wire format specified
    by the gRPC protocol."""

    def test_compressed_flag_is_zero_for_uncompressed(self):
        """Compressed-Flag byte MUST be 0 when ``compressed=False`` (default)."""
        framed = encode_message(b'hello')
        assert framed[0] == 0, 'uncompressed flag must be 0'

    def test_compressed_flag_is_one_when_compressed(self):
        """Compressed-Flag byte MUST be 1 when ``compressed=True``."""
        framed = encode_message(b'hello', compressed=True)
        assert framed[0] == 1, 'compressed flag must be 1 when set'

    def test_message_length_is_big_endian_uint32(self):
        """Message-Length MUST be a 4-byte big-endian unsigned integer."""
        payload = b'x' * 300
        framed = encode_message(payload)
        decoded_len = int.from_bytes(framed[1:5], 'big')
        assert decoded_len == 300

    def test_empty_message_produces_valid_frame(self):
        """An empty payload (length 0) MUST produce a valid frame with
        zero body bytes."""
        framed = encode_message(b'')
        assert len(framed) == 5
        assert framed[0] == 0
        assert framed[1:5] == b'\x00\x00\x00\x00'

    def test_payload_matches_input_exactly(self):
        """The message body after the 5-byte prefix MUST match the input
        bytes exactly — no transformation, no padding."""
        import os
        payload = os.urandom(256)
        framed = encode_message(payload)
        assert framed[5:] == payload

    def test_very_large_message_encodes_correctly(self):
        """A message of 2^24 bytes still encodes with correct length prefix."""
        payload = b'\x00' * (2 ** 24)  # 16 MiB
        framed = encode_message(payload)
        assert framed[0] == 0
        assert int.from_bytes(framed[1:5], 'big') == 2 ** 24
        assert len(framed) == 5 + 2 ** 24

    def test_single_byte_payload(self):
        """A 1-byte payload must have length prefix 1."""
        framed = encode_message(b'X')
        assert int.from_bytes(framed[1:5], 'big') == 1
        assert framed[5:] == b'X'


# ---------------------------------------------------------------------------
# §2 — Decoding correctness (RFC: wire format)
# ---------------------------------------------------------------------------

class TestDecodeWireFormat:
    """Verify that ``decode_messages`` correctly parses valid message sequences
    and faithfully reports the compressed flag."""

    def test_empty_buffer_yields_no_messages(self):
        """A zero-length buffer contains zero messages — not an error."""
        assert decode_messages(b'') == []

    def test_single_message_round_trip(self):
        """encode → decode must be identity for the payload."""
        for payload in [b'', b'hello', b'x' * 1024, bytes(range(256))]:
            framed = encode_message(payload)
            decoded = decode_messages(framed)
            assert decoded == [(False, payload)], f'round-trip failed for {payload!r}'

    def test_multiple_messages_in_one_buffer(self):
        """gRPC allows multiple framed messages in a single DATA buffer.
        All must be decoded in order."""
        buf = encode_message(b'one') + encode_message(b'two') + encode_message(b'three')
        assert decode_messages(buf) == [
            (False, b'one'), (False, b'two'), (False, b'three'),
        ]

    def test_messages_are_not_required_to_align_to_frame_boundaries(self):
        """The spec says frames may be fragmented arbitrarily across DATA
        frames.  The decoder does not track frame boundaries — it just
        consumes the buffer as a byte stream."""
        # Simulate: message1 header split from body, then message2 complete
        m1 = encode_message(b'first')
        m2 = encode_message(b'second')
        chunked = m1[:3] + m1[3:] + m2
        assert decode_messages(chunked) == [
            (False, b'first'), (False, b'second'),
        ]

    def test_compressed_flag_is_propagated(self):
        """The compressed flag (bool) MUST be reported faithfully per-message."""
        buf = encode_message(b'a', compressed=True) + encode_message(b'b', compressed=False)
        assert decode_messages(buf) == [(True, b'a'), (False, b'b')]

    def test_zero_length_message_in_middle(self):
        """A zero-length message between two non-empty messages is valid."""
        buf = encode_message(b'one') + encode_message(b'') + encode_message(b'two')
        assert decode_messages(buf) == [
            (False, b'one'), (False, b''), (False, b'two'),
        ]


# ---------------------------------------------------------------------------
# §3 — Error conditions (malformed wire data)
# ---------------------------------------------------------------------------

class TestDecodeErrorHandling:
    """Every violation of the wire format MUST raise ``GrpcDecodeError`` —
    the absence of an error for malformed input is a conformance bug."""

    def test_truncated_prefix_fewer_than_5_bytes(self):
        """A partial prefix (< 5 bytes) is a decoding error."""
        for n in range(1, 5):
            with pytest.raises(GrpcDecodeError, match='truncated'):
                decode_messages(b'\x00' * n)

    def test_truncated_body(self):
        """Declared length > available bytes → error."""
        with pytest.raises(GrpcDecodeError, match='truncated'):
            decode_messages(b'\x00' + (10).to_bytes(4, 'big') + b'abc')

    def test_declared_length_exceeds_buffer_by_one(self):
        """Off-by-one truncation must still be caught."""
        with pytest.raises(GrpcDecodeError):
            decode_messages(b'\x00' + (5).to_bytes(4, 'big') + b'four')

    def test_body_exactly_matches_declared_length_succeeds(self):
        """When available bytes exactly equal declared length, it's valid."""
        # This must NOT raise.
        result = decode_messages(b'\x00' + (5).to_bytes(4, 'big') + b'hello')
        assert result == [(False, b'hello')]

    def test_trailing_truncated_message_after_valid_one(self):
        """A valid message followed by a truncated prefix for the next."""
        valid = encode_message(b'ok')
        with pytest.raises(GrpcDecodeError):
            decode_messages(valid + b'\x00\x00')

    def test_second_message_truncated(self):
        """First message complete, second message body truncated."""
        buf = encode_message(b'ok') + b'\x00' + (100).to_bytes(4, 'big') + b'short'
        with pytest.raises(GrpcDecodeError):
            decode_messages(buf)


# ---------------------------------------------------------------------------
# §4 — Security: oversized / malicious length values
# ---------------------------------------------------------------------------

class TestDecodeSecurityBoundaries:
    """The decoder must not be vulnerable to declared lengths that could cause
    OOM or integer overflow.  These are not typical "happy path" tests."""

    def test_huge_declared_length_does_not_crash(self):
        """A declared length near 2^31-1 should raise GrpcDecodeError,
        not allocate and not segfault."""
        huge = b'\x00' + (2 ** 31 - 1).to_bytes(4, 'big') + b'x' * 100
        with pytest.raises(GrpcDecodeError):
            decode_messages(huge)

    def test_max_uint32_declared_length_is_handled(self):
        """Declared length of 0xFFFFFFFF (4 GiB) must be handled gracefully."""
        max_len = b'\x00' + b'\xff\xff\xff\xff' + b'x' * 20
        with pytest.raises(GrpcDecodeError):
            decode_messages(max_len)

    def test_zero_length_message_is_valid(self):
        """A declared length of 0 with no body bytes is perfectly valid."""
        result = decode_messages(b'\x00\x00\x00\x00\x00')
        assert result == [(False, b'')]

    def test_compressed_flag_beyond_one(self):
        """Values > 1 for the compressed flag byte are not valid per spec,
        but the decoder reports them faithfully (the caller rejects)."""
        # Custom-prefix: flag=0xFF, length=0
        framed = b'\xff\x00\x00\x00\x00'
        result = decode_messages(framed)
        assert result == [(True, b'')]  # any non-zero → True


# ---------------------------------------------------------------------------
# §5 — Multiple-message buffer edge cases
# ---------------------------------------------------------------------------

class TestMultiMessageEdgeCases:
    """The gRPC spec permits multiple messages in a single DATA buffer.
    Verify the decoder handles various message-count scenarios."""

    def test_exactly_100_messages(self):
        """Stress-test: 100 back-to-back messages."""
        buf = b''.join(encode_message(bytes([i])) for i in range(100))
        result = decode_messages(buf)
        assert len(result) == 100
        for i, (compressed, payload) in enumerate(result):
            assert compressed is False
            assert payload == bytes([i])

    def test_alternating_empty_and_non_empty(self):
        """Alternating empty/non-empty messages."""
        buf = encode_message(b'') + encode_message(b'a') + encode_message(b'') + encode_message(b'b')
        assert decode_messages(buf) == [
            (False, b''), (False, b'a'), (False, b''), (False, b'b'),
        ]

    def test_all_empty_messages(self):
        """A buffer containing only zero-length messages."""
        buf = encode_message(b'') * 5
        assert decode_messages(buf) == [(False, b'')] * 5
