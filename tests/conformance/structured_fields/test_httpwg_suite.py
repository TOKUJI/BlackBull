"""Run the vendored httpwg/structured-field-tests suite against
``blackbull.protocol.structured_fields`` (RFC 9651).

Suite layout (see ``data/README.md`` for the pinned commit):

- ``data/*.json`` — parse tests: ``raw`` field lines (joined with ``", "``)
  are parsed as ``header_type``; ``expected`` is the typed JSON encoding of
  the result; ``must_fail`` means the parse MUST error; ``can_fail`` means
  an error is acceptable.  When parsing succeeds, re-serialising the result
  must yield ``canonical`` (defaults to the joined ``raw``).
- ``data/serialisation-tests/*.json`` — serialisation-only tests:
  ``expected`` is serialised and compared to ``canonical``; ``must_fail``
  means serialisation MUST error.

Typed JSON encoding of bare items: JSON bool/int/float/string map directly;
``{"__type": "token"|"binary"|"date"|"displaystring", "value": ...}`` wrap
the rest (``binary`` values are base32-encoded).  An Item is
``[bare, params]``, params ``[[key, bare], ...]``; an Inner List is
``[[item, ...], params]``; a List is ``[member, ...]``; a Dictionary is
``[[key, member], ...]``.
"""
import base64
import json
from pathlib import Path

import pytest

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

DATA = Path(__file__).parent / 'data'

_PARSERS = {
    'item': parse_item,
    'list': parse_list,
    'dictionary': parse_dictionary,
}
_SERIALIZERS = {
    'item': serialize_item,
    'list': serialize_list,
    'dictionary': serialize_dictionary,
}


# ---- typed-JSON → Python conversion ----------------------------------------

def _bare(v):
    if isinstance(v, dict):
        t, value = v['__type'], v['value']
        if t == 'token':
            return Token(value)
        if t == 'binary':
            return base64.b32decode(value)
        if t == 'date':
            return Date(value)
        if t == 'displaystring':
            return DisplayString(value)
        raise AssertionError(f'unknown typed value: {t}')
    return v


def _params(pairs):
    return {k: _bare(v) for k, v in pairs}


def _member(m):
    """Item ``[bare, params]`` or Inner List ``[[item, ...], params]``."""
    value, params = m
    if isinstance(value, list):
        return ([_member(i) for i in value], _params(params))
    return (_bare(value), _params(params))


def _expected(header_type, expected):
    if header_type == 'item':
        return _member(expected)
    if header_type == 'list':
        return [_member(m) for m in expected]
    return {k: _member(m) for k, m in expected}


# ---- strict comparison ------------------------------------------------------

def _kind(v):
    """Canonical bare-type tag; distinguishes bool/int and Token/str."""
    if isinstance(v, bool):
        return 'boolean'
    if isinstance(v, int):
        return 'integer'
    if isinstance(v, float):
        return 'decimal'
    if isinstance(v, Token):
        return 'token'
    if isinstance(v, DisplayString):
        return 'displaystring'
    if isinstance(v, str):
        return 'string'
    if isinstance(v, bytes):
        return 'binary'
    if isinstance(v, Date):
        return 'date'
    raise AssertionError(f'unexpected bare type: {v!r}')


def _assert_bare_equal(got, want):
    assert _kind(got) == _kind(want), f'{got!r} vs {want!r}'
    assert got == want


def _assert_params_equal(got, want):
    assert list(got) == list(want)          # key order matters
    for k in want:
        _assert_bare_equal(got[k], want[k])


def _assert_member_equal(got, want):
    want_value, want_params = want
    got_value, got_params = got
    if isinstance(want_value, list):        # inner list
        assert isinstance(got_value, list)
        assert len(got_value) == len(want_value)
        for g, w in zip(got_value, want_value):
            _assert_member_equal(g, w)
    else:
        _assert_bare_equal(got_value, want_value)
    _assert_params_equal(got_params, want_params)


def _assert_equal(header_type, got, want):
    if header_type == 'item':
        _assert_member_equal(got, want)
    elif header_type == 'list':
        assert len(got) == len(want)
        for g, w in zip(got, want):
            _assert_member_equal(g, w)
    else:
        assert list(got) == list(want)      # key order matters
        for k in want:
            _assert_member_equal(got[k], want[k])


# ---- case collection --------------------------------------------------------

def _load(directory):
    cases = []
    for path in sorted(directory.glob('*.json')):
        for case in json.loads(path.read_text()):
            cases.append(pytest.param(case, id=f'{path.stem}: {case["name"]}'))
    return cases


PARSE_CASES = _load(DATA)
SERIALIZE_CASES = _load(DATA / 'serialisation-tests')


# ---- tests -------------------------------------------------------------------

@pytest.mark.parametrize('case', PARSE_CASES)
def test_parse(case):
    parse = _PARSERS[case['header_type']]
    # Non-ASCII raw (always must_fail/can_fail cases) reaches the parser as
    # non-ASCII bytes, which RFC 9651 requires it to reject.
    raw = ', '.join(case['raw']).encode('utf-8')
    try:
        got = parse(raw)
    except ValueError:
        assert case.get('must_fail') or case.get('can_fail'), \
            f'unexpected parse failure for {raw!r}'
        return
    assert not case.get('must_fail'), f'parse should have failed for {raw!r}'
    _assert_equal(case['header_type'],
                  got, _expected(case['header_type'], case['expected']))

    # Round-trip: re-serialising the parsed value must yield the canonical form.
    canonical = case.get('canonical', case['raw'])
    serialize = _SERIALIZERS[case['header_type']]
    if case['header_type'] == 'item':
        out = serialize(got[0], got[1])
    else:
        out = serialize(got)
    assert out == ', '.join(canonical).encode()


@pytest.mark.parametrize('case', SERIALIZE_CASES)
def test_serialize(case):
    serialize = _SERIALIZERS[case['header_type']]
    value = _expected(case['header_type'], case['expected'])
    try:
        if case['header_type'] == 'item':
            out = serialize(value[0], value[1])
        else:
            out = serialize(value)
    except ValueError:
        assert case.get('must_fail') or case.get('can_fail'), \
            f'unexpected serialise failure for {case["name"]}'
        return
    assert not case.get('must_fail'), \
        f'serialise should have failed for {case["name"]}'
    canonical = case.get('canonical', case.get('raw'))
    if canonical is not None:
        assert out == ', '.join(canonical).encode()
