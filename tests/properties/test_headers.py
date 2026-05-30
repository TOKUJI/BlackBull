"""Property tests for blackbull.server.headers.Headers.

The existing unit tests in tests/unit/test_headers.py cover specific examples.
These property tests verify structural invariants across arbitrary byte inputs.
"""
import pytest
from hypothesis import given
from hypothesis import strategies as st

from blackbull.headers import Headers
from .strategies import header_name, header_value


@pytest.mark.properties
@given(name=header_name, value=header_value)
def test_case_insensitive_lookup(name, value):
    """Headers lookup must return the same value for any ASCII case variant of the name."""
    h = Headers([(name, value)])
    assert h.get(name.lower()) == value
    assert h.get(name.upper()) == value


@pytest.mark.properties
@given(pairs=st.lists(st.tuples(header_name, header_value), min_size=1, max_size=16))
def test_all_inserted_pairs_are_reachable(pairs):
    """Every (name, value) pair inserted must appear in the iteration output."""
    h = Headers(pairs)
    iterated = list(h)
    for name, value in pairs:
        assert (name, value) in iterated


@pytest.mark.properties
@given(
    p1=st.lists(st.tuples(header_name, header_value), max_size=8),
    p2=st.lists(st.tuples(header_name, header_value), max_size=8),
)
def test_concatenation_contains_all_pairs(p1, p2):
    """(h1 + h2) must contain every pair from h1 and every pair from h2."""
    h1, h2 = Headers(p1), Headers(p2)
    combined = list(h1 + h2)
    for pair in p1:
        assert pair in combined
    for pair in p2:
        assert pair in combined
