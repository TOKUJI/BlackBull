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

    def test_window_bits_out_of_range_skips_offer(self):
        assert negotiate(b'permessage-deflate; server_max_window_bits=7') == (None, None)
        assert negotiate(b'permessage-deflate; client_max_window_bits=16') == (None, None)


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


class TestNegotiationDeclinesInvalidOffers:
    """RFC 7692 §7.1.1 — these offers a server MUST decline."""

    @pytest.mark.parametrize('offer', [
        pytest.param(b'permessage-deflate; x-mystery=1', id='unknown-parameter'),
        pytest.param(b'permessage-deflate; client_max_window_bits=10'
                     b'; client_max_window_bits=12', id='duplicate-parameter'),
        pytest.param(b'permessage-deflate; server_max_window_bits',
                     id='valueless-server-max-window-bits'),
        pytest.param(b'permessage-deflate; server_no_context_takeover=1',
                     id='valued-no-context-takeover'),
        pytest.param(b'x-other-extension', id='other-extension-only'),
        pytest.param(b'permessage-deflate; server_max_window_bits=oops',
                     id='malformed-window-bits-value'),
    ])
    def test_a_duplicate_parameter_declines_the_offer(self, offer):
        """RFC 7692 §7.1.1 — these offers a server MUST decline."""
        assert negotiate(offer) == (None, None)

    @pytest.mark.parametrize('value', [b'08', b'+8', b'8x', b'"8x"', b'""', b'7', b'16'])
    def test_a_window_value_outside_bare_8_to_15_declines_the_offer(self, value):
        assert negotiate(b'permessage-deflate; server_max_window_bits=' + value) == (None, None)

    def test_an_invalid_offer_falls_through_to_the_next_valid_one(self):
        params, response = negotiate(
            b'permessage-deflate; x-mystery=1, '
            b'permessage-deflate; client_no_context_takeover')
        assert params == DeflateParams(client_no_context_takeover=True)
        assert response == b'permessage-deflate; client_no_context_takeover'


class TestNegotiationQuotedValues:
    def test_a_quoted_window_value_is_unquoted(self):
        params, _ = negotiate(b'permessage-deflate; server_max_window_bits="14"')
        assert params is not None
        assert params.server_max_window_bits == 14


class TestNegotiationMatchesRuntimeSupport:
    """Accept only configurations whose codecs the runtime serves (§7.1.1)."""

    @staticmethod
    def _pair_works(server_wbits, client_wbits):
        try:
            out = OutboundCompressor(server_wbits, reset_per_message=False)
            inc = InboundDecompressor(client_wbits, reset_per_message=False)
        except ValueError:
            return False
        return inc.decompress(out.compress(b'ping')) == b'ping'

    @pytest.mark.parametrize('wbits', range(8, 16))
    def test_an_accepted_window_is_one_the_runtime_serves(self, wbits):
        params, _ = negotiate(
            f'permessage-deflate; server_max_window_bits={wbits}'.encode())
        if self._pair_works(wbits, 15):
            assert params is not None
            assert params.server_max_window_bits == wbits
        else:
            assert params is None

    def test_an_unusable_window_is_declined_not_raised(self):
        if self._pair_works(8, 15):
            pytest.skip('this zlib serves window 8; the decline path is unreachable')
        assert negotiate(b'permessage-deflate; server_max_window_bits=8') == (None, None)
        params, _ = negotiate(
            b'permessage-deflate; server_max_window_bits=8, '
            b'permessage-deflate; client_max_window_bits=10')
        assert params == DeflateParams(client_max_window_bits=10)


class TestResponseCarriesOnlyOfferedConstraints:
    def test_window_constraints_appear_only_when_the_offer_had_them(self):
        _, response = negotiate(b'permessage-deflate; client_no_context_takeover')
        assert response == b'permessage-deflate; client_no_context_takeover'
        _, response = negotiate(b'permessage-deflate; client_max_window_bits=10')
        assert response == b'permessage-deflate; client_max_window_bits=10'
