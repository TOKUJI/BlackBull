"""Shared Hypothesis strategies for BlackBull property tests."""
from hypothesis import strategies as st

json_scalar = st.one_of(
    st.none(),
    st.booleans(),
    st.integers(),
    st.floats(allow_nan=False, allow_infinity=False),
    st.text(),
)

json_value = st.recursive(
    json_scalar,
    lambda children: st.lists(children) | st.dictionaries(st.text(), children),
    max_leaves=10,
)

ws_payload = st.binary(max_size=65536)

ws_mask_key = st.binary(min_size=4, max_size=4)

# Valid HTTP/2 stream IDs: odd numbers initiated by client (RFC 7540 §5.1.1).
# For round-trip frame tests any non-zero value is fine.
stream_id = st.integers(min_value=1, max_value=2**31 - 1)

header_name = st.binary(min_size=1, max_size=64).map(bytes.lower)

header_value = st.binary(min_size=0, max_size=256)
