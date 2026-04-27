import pytest
from blackbull.server.headers import Headers


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
