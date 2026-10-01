"""The one reading of a ``Transfer-Encoding`` list (BLA-459).  Each side's
policy over it keeps its own tests, in ``tests/unit/`` and ``test_audit_sprint63``.
"""
import pytest

from blackbull.protocol.framing import split_transfer_codings


class TestDecomposition:
    def test_commas_split_and_ows_comes_off(self):
        assert split_transfer_codings(
            [(b'transfer-encoding', b'chunked ,\tgzip')]) == [
                (b'chunked', ()), (b'gzip', ())]

    def test_the_coding_token_is_lowered(self):
        assert split_transfer_codings(
            [(b'transfer-encoding', b'GZip, ChUnKeD')]) == [
                (b'gzip', ()), (b'chunked', ())]

    def test_parameters_are_kept_not_dropped(self):
        assert split_transfer_codings(
            [(b'transfer-encoding', b'chunked; ext=1')]) == [
                (b'chunked', ((b'ext', b'1'),))]

    def test_parameter_names_and_values_stay_as_written(self):
        # Only the coding token is lowered; parameters are verbatim.
        assert split_transfer_codings(
            [(b'transfer-encoding', b'GZip;P="a,B"')]) == [
                (b'gzip', ((b'P', b'"a,B"'),))]

    def test_a_quoted_parameter_value_keeps_its_commas_and_quotes(self):
        # A quoted comma is not a member separator; the value keeps its quotes.
        assert split_transfer_codings(
            [(b'transfer-encoding', b'gzip;parameter="a,b", chunked')]) == [
                (b'gzip', ((b'parameter', b'"a,b"'),)), (b'chunked', ())]

    def test_an_empty_member_stays_a_member(self):
        # A sender counts them and a reader ignores them (RFC 9110 §5.6.1):
        # the reading keeps both decisions possible.
        assert split_transfer_codings(
            [(b'transfer-encoding', b'chunked, ')]) == [
                (b'chunked', ()), (b'', ())]
        assert split_transfer_codings(
            [(b'transfer-encoding', b'')]) == [(b'', ())]

    def test_repeated_fields_flatten_in_order(self):
        assert split_transfer_codings([
            (b'transfer-encoding', b'gzip,'),
            (b'transfer-encoding', b',chunked'),
        ]) == [(b'gzip', ()), (b'', ()), (b'', ()), (b'chunked', ())]


class TestRefusals:
    @pytest.mark.parametrize('value', [
        b'gzip;bad',               # a parameter with no "="
        b'gzip;parameter=',        # an "=" with no value
        b'gzip;=value',            # an "=" with no parameter name
        b'gzip parameter=value',   # a parameter with no ";"
        b'gzip;parameter="unterminated',
        b'gzip;parameter="a\x00b"',
    ])
    def test_a_member_that_is_not_the_grammar_raises(self, value):
        # An unseen-through parameter must not hide a different final coding.
        with pytest.raises(ValueError):
            split_transfer_codings([(b'transfer-encoding', value)])

    def test_a_reasonable_number_of_empty_members_is_bounded(self):
        assert len(split_transfer_codings(
            [(b'transfer-encoding', b',' * 15)])) == 16
        with pytest.raises(ValueError):
            split_transfer_codings([(b'transfer-encoding', b',' * 16)])

    def test_the_bound_counts_empty_members_across_fields(self):
        # The reading is of the whole field section: 8 + 9 empties are 17.
        with pytest.raises(ValueError):
            split_transfer_codings([
                (b'transfer-encoding', b',' * 7),
                (b'transfer-encoding', b',' * 8),
            ])
