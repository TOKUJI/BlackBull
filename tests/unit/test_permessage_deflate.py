"""Unit tests for RFC 7692 permessage-deflate negotiation + per-message codec.

The full wire-level conformance is exercised by Autobahn|Testsuite (sections
12 and 13).  These tests focus on the bits of policy that don't surface in
Autobahn: how we parse offers with multiple parameters and how we decide
when to decline.
"""
import pytest
import zlib

from blackbull.server.permessage_deflate import (
    DeflateParams,
    InboundDecompressor,
    OutboundCompressor,
    negotiate,
)


class TestNegotiateAccepts:
    """Offers we accept and the response header we render."""

    def test_bare_offer_accepts_with_default_window_bits(self):
        params, response = negotiate(b'permessage-deflate')
        assert params == DeflateParams()
        assert response == b'permessage-deflate'

    def test_no_context_takeover_offer_honored(self):
        params, response = negotiate(
            b'permessage-deflate; client_no_context_takeover')
        assert params.client_no_context_takeover is True
        assert params.server_no_context_takeover is False
        assert b'client_no_context_takeover' in response

    def test_client_max_window_bits_no_value_is_a_hint(self):
        """``client_max_window_bits`` without a value advertises support; the
        server may choose any value and is free to omit the param in reply."""
        params, response = negotiate(
            b'permessage-deflate; client_max_window_bits')
        assert params.client_max_window_bits == 15
        # Default 15 → not emitted in the response
        assert b'client_max_window_bits' not in response

    def test_explicit_window_bits_value_accepted(self):
        params, response = negotiate(
            b'permessage-deflate; server_max_window_bits=12')
        assert params.server_max_window_bits == 12
        assert b'server_max_window_bits=12' in response

    def test_first_acceptable_offer_wins(self):
        """Multiple offers: pick the first ``permessage-deflate`` we can satisfy."""
        params, _ = negotiate(
            b'foo, permessage-deflate; client_no_context_takeover, permessage-deflate')
        assert params.client_no_context_takeover is True


class TestNegotiateDeclines:
    """Inputs that should not produce an acceptance."""

    def test_absent_header_declines(self):
        assert negotiate(None) == (None, None)
        assert negotiate(b'') == (None, None)

    def test_other_extension_only_declines(self):
        assert negotiate(b'x-other-extension') == (None, None)

    def test_window_bits_out_of_range_skips_offer(self):
        assert negotiate(b'permessage-deflate; server_max_window_bits=7') == (None, None)
        assert negotiate(b'permessage-deflate; client_max_window_bits=16') == (None, None)

    def test_malformed_window_bits_value_skips_offer(self):
        assert negotiate(b'permessage-deflate; server_max_window_bits=oops') == (None, None)


class TestRoundTripCompression:
    """An OutboundCompressor's output must be decompressible by zlib (with the
    trailing 0x00 0x00 0xff 0xff appended back) — that's the protocol contract.
    The same property in reverse for InboundDecompressor."""

    def test_compressor_output_inflates_to_input(self):
        compressor = OutboundCompressor(wbits=15, reset_per_message=False)
        original = b'Hello, ' * 100
        compressed = compressor.compress(original)
        assert compressed != original
        # Peer-side inflate (raw deflate; tail appended back per RFC 7692 §7.2)
        inflater = zlib.decompressobj(wbits=-15)
        assert inflater.decompress(compressed + b'\x00\x00\xff\xff') == original

    def test_decompressor_inflates_what_zlib_deflated(self):
        decompressor = InboundDecompressor(wbits=15, reset_per_message=False)
        original = b'The quick brown fox' * 50
        deflater = zlib.compressobj(wbits=-15)
        compressed = deflater.compress(original) + deflater.flush(zlib.Z_SYNC_FLUSH)
        # RFC 7692 §7.2.1 — strip the trailing 4 bytes before sending
        assert compressed.endswith(b'\x00\x00\xff\xff')
        compressed = compressed[:-4]
        assert decompressor.decompress(compressed) == original

    def test_context_takeover_yields_better_compression_than_reset(self):
        """Without context takeover, each message restarts the deflate dict,
        so identical repeated payloads compress about the same.  *With*
        context takeover, the second message benefits from the dictionary
        learned on the first.
        """
        payload = b'BlackBull is an ASGI 3.0 server. ' * 80
        with_ctxt = OutboundCompressor(wbits=15, reset_per_message=False)
        without_ctxt = OutboundCompressor(wbits=15, reset_per_message=True)
        _ = with_ctxt.compress(payload)
        with_second = with_ctxt.compress(payload)
        _ = without_ctxt.compress(payload)
        without_second = without_ctxt.compress(payload)
        assert len(with_second) < len(without_second), (
            'context takeover should improve the second compression')

    def test_reset_per_message_recreates_decompressor_state(self):
        """A no-context-takeover decompressor must not carry trailing state
        from the previous message into the next inflater.
        """
        decompressor = InboundDecompressor(wbits=15, reset_per_message=True)
        for _ in range(3):
            deflater = zlib.compressobj(wbits=-15)
            compressed = deflater.compress(b'message') + deflater.flush(zlib.Z_SYNC_FLUSH)
            assert decompressor.decompress(compressed[:-4]) == b'message'


class TestInvalidCompressedData:
    def test_garbage_payload_raises_zlib_error(self):
        decompressor = InboundDecompressor(wbits=15, reset_per_message=False)
        with pytest.raises(Exception):
            decompressor.decompress(b'\xff' * 32)
