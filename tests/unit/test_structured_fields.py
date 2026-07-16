"""Unit tests for ``blackbull.protocol.structured_fields`` API behaviour
not covered by the vendored httpwg conformance suite
(``tests/conformance/structured_fields/``): accepted input types, wrapper
type semantics, and serialiser type validation.
"""
from datetime import datetime, timezone

import pytest

try:
    from beartype.roar import BeartypeCallHintParamViolation as _BeartypeViolation
except ImportError:
    _BeartypeViolation = None
_TYPE_ERRORS = (TypeError,) if _BeartypeViolation is None else (TypeError, _BeartypeViolation)

from blackbull.protocol.structured_fields import (
    Date,
    DisplayString,
    Token,
    parse_dictionary,
    parse_item,
    parse_list,
    serialize_dictionary,
    serialize_item,
    serialize_list,
)


# ---- input types -----------------------------------------------------------

@pytest.mark.parametrize('value', [b'u=2, i', bytearray(b'u=2, i'), 'u=2, i'])
def test_parse_accepts_bytes_bytearray_str(value):
    assert parse_dictionary(value) == {'u': (2, {}), 'i': (True, {})}


def test_parse_rejects_non_ascii_bytes():
    with pytest.raises(ValueError):
        parse_item(b'"caf\xc3\xa9"')


def test_parse_rejects_non_ascii_str():
    with pytest.raises(ValueError):
        parse_item('"café"')


def test_serialize_returns_ascii_bytes():
    out = serialize_item(Token('foo'), {'a': 1})
    assert isinstance(out, bytes)
    assert out == b'foo;a=1'


# ---- wrapper types ---------------------------------------------------------

def test_token_and_display_string_are_distinct_from_str():
    token, _ = parse_item(b'foo')
    string, _ = parse_item(b'"foo"')
    display, _ = parse_item(b'%"foo"')
    assert isinstance(token, Token)
    assert isinstance(display, DisplayString)
    assert type(string) is str
    # Equal as str, distinguishable by type — serialisation must differ.
    assert token == string == display
    assert serialize_item(token) == b'foo'
    assert serialize_item(string) == b'"foo"'
    assert serialize_item(display) == b'%"foo"'


def test_date_datetime_round_trip():
    dt = datetime(2022, 8, 4, 1, 57, 13, tzinfo=timezone.utc)
    assert Date.from_datetime(dt).to_datetime() == dt
    assert Date.from_datetime(dt) == Date(1659578233)


def test_date_naive_datetime_is_utc():
    assert Date.from_datetime(datetime(1970, 1, 1)) == Date(0)


def test_date_beyond_datetime_range():
    # SF Dates cover ±(10^15 - 1) seconds; datetime cannot represent that,
    # which is why Date wraps an int instead of subclassing datetime.
    big = Date(999_999_999_999_999)
    assert parse_item(b'@999999999999999') == (big, {})
    assert serialize_item(big) == b'@999999999999999'
    with pytest.raises((OverflowError, ValueError, OSError)):
        big.to_datetime()


def test_date_rejects_non_int():
    with pytest.raises(_TYPE_ERRORS):
        Date(True)
    with pytest.raises(_TYPE_ERRORS):
        Date(1.5)


def test_date_equality_and_hash():
    assert Date(5) == Date(5)
    assert Date(5) != Date(6)
    assert Date(5) != 5
    assert hash(Date(5)) == hash(Date(5))


# ---- serialiser type validation ---------------------------------------------

def test_serialize_rejects_unmapped_type():
    # Plain run: the serialiser raises ValueError; under beartype the
    # signature check rejects the argument first.
    with pytest.raises((ValueError, *_TYPE_ERRORS)):
        serialize_item(object())


def test_serialize_rejects_invalid_token():
    with pytest.raises(ValueError):
        serialize_item(Token('not a token'))


def test_serialize_bool_before_int():
    # bool is an int subclass; it must serialise as ?1/?0, never as 1/0.
    assert serialize_item(True) == b'?1'
    assert serialize_list([(True, {}), (1, {})]) == b'?1, 1'


def test_serialize_dictionary_true_member_omits_value():
    assert serialize_dictionary({'u': (2, {}), 'i': (True, {})}) == b'u=2, i'


def test_serialize_empty_containers_yield_empty_bytes():
    # Per RFC 9651 §3.1 the field should then not be emitted at all.
    assert serialize_list([]) == b''
    assert serialize_dictionary({}) == b''


def test_round_trip_composite():
    raw = b'a=(1 2.5 tok);x, b="s";y=:aGk=:, c=@0, d=%"f%c3%bc"'
    parsed = parse_dictionary(raw)
    assert serialize_dictionary(parsed) == raw
    list_value = [(b'hi', {}), ([(1, {})], {'q': 0.5})]
    assert parse_list(serialize_list(list_value)) == list_value
