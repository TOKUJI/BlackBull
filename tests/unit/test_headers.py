import pytest
from blackbull.headers import Headers


@pytest.fixture
def h():
    return Headers([(b'content-type', b'text/html'), (b'x-custom', b'a')])


def test_len(h):
    assert len(h) == 2


def test_len_empty():
    assert len(Headers([])) == 0


def test_getitem_present(h):
    result = h[b'content-type']
    assert result == [(b'content-type', b'text/html')]


def test_getitem_case_insensitive(h):
    result = h[b'Content-Type']
    assert result == [(b'content-type', b'text/html')]


def test_getitem_missing_raises(h):
    with pytest.raises(KeyError):
        _ = h[b'x-nonexistent']


def test_append_iterable(h):
    h.append([(b'x-extra', b'1'), (b'x-extra', b'2')])
    assert len(h) == 4
    assert h[b'x-extra'] == [(b'x-extra', b'1'), (b'x-extra', b'2')]


def test_append_iterable_updates_index():
    headers = Headers([])
    headers.append([(b'set-cookie', b'a=1'), (b'set-cookie', b'b=2')])
    assert len(headers.getlist(b'set-cookie')) == 2


def test_add_returns_new_headers():
    h1 = Headers([(b'a', b'1')])
    h2 = Headers([(b'b', b'2')])
    combined = h1 + h2
    assert isinstance(combined, Headers)
    assert len(combined) == 2
    assert combined[b'a'] == [(b'a', b'1')]
    assert combined[b'b'] == [(b'b', b'2')]


def test_add_does_not_mutate_originals():
    h1 = Headers([(b'a', b'1')])
    h2 = Headers([(b'b', b'2')])
    _ = h1 + h2
    assert len(h1) == 1
    assert len(h2) == 1


# ---- Structured Fields accessors (RFC 9651) -------------------------------

from blackbull.protocol.structured_fields import Date, Token


def test_get_sf_item():
    h = Headers([(b'deprecation', b'@1659578233')])
    assert h.get_sf_item(b'deprecation') == (Date(1659578233), {})


def test_get_sf_item_with_params():
    h = Headers([(b'x-thing', b'5; foo=bar')])
    assert h.get_sf_item(b'x-thing') == (5, {'foo': Token('bar')})


def test_get_sf_item_absent_returns_none():
    assert Headers([]).get_sf_item(b'deprecation') is None


def test_get_sf_item_malformed_returns_none():
    h = Headers([(b'deprecation', b'@later')])
    assert h.get_sf_item(b'deprecation') is None


def test_get_sf_item_case_insensitive():
    h = Headers([(b'Deprecation', b'?1')])
    assert h.get_sf_item(b'deprecation') == (True, {})


def test_get_sf_list_combines_field_lines():
    # RFC 9651 §4.2 — multiple field lines are combined before parsing.
    h = Headers([(b'accept-query', b'"a", "b"'), (b'accept-query', b'"c"')])
    assert h.get_sf_list(b'accept-query') == [('a', {}), ('b', {}), ('c', {})]


def test_get_sf_list_inner_list():
    h = Headers([(b'x-list', b'(1 2);q=1.0, 3')])
    assert h.get_sf_list(b'x-list') == [
        ([(1, {}), (2, {})], {'q': 1.0}),
        (3, {}),
    ]


def test_get_sf_list_absent_returns_none():
    assert Headers([]).get_sf_list(b'accept-query') is None


def test_get_sf_list_malformed_returns_none():
    h = Headers([(b'accept-query', b'"unterminated')])
    assert h.get_sf_list(b'accept-query') is None


def test_get_sf_dict():
    h = Headers([(b'priority', b'u=2, i')])
    assert h.get_sf_dict(b'priority') == {'u': (2, {}), 'i': (True, {})}


def test_get_sf_dict_combines_field_lines():
    h = Headers([(b'priority', b'u=2'), (b'priority', b'i')])
    assert h.get_sf_dict(b'priority') == {'u': (2, {}), 'i': (True, {})}


def test_get_sf_dict_absent_returns_none():
    assert Headers([]).get_sf_dict(b'priority') is None


def test_get_sf_dict_malformed_returns_none():
    # Strict parsing: one bad member poisons the whole field (RFC 9651 §1.1).
    h = Headers([(b'priority', b'u=2, ???')])
    assert h.get_sf_dict(b'priority') is None


def test_get_sf_item_multiple_lines_of_single_item_is_malformed():
    # Joining two lines of an Item-typed field cannot parse as one Item.
    h = Headers([(b'x-thing', b'1'), (b'x-thing', b'2')])
    assert h.get_sf_item(b'x-thing') is None
