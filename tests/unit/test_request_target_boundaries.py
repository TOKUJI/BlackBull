"""Request-target components are separated before percent decoding."""
import pytest
from hypothesis import given, strategies as st

from blackbull.connection import Connection
from blackbull.server.http1_actor import HTTP1Actor
from blackbull.server.parser import parse_headers
from tests.unit.test_parser import _http_request, _make_h2_headers_frame_dispatch


def _connections(target):
    h1 = object.__new__(HTTP1Actor)._parse(_http_request(path=target))
    h2 = parse_headers(_make_h2_headers_frame_dispatch(path=target))
    assert h2 is not None
    return h1, h2


def _assert_components(conn, decoded, raw, query):
    assert (conn.path, conn.raw_path, conn.query_string) == (decoded, raw, query)
    scope = conn.as_scope()
    assert (scope['path'], scope['raw_path'], scope['query_string']) == (decoded, raw, query)
    assert scope['server'] == (list(conn.server) if conn.server is not None else None)
    restored = Connection.from_scope(scope)
    assert (restored.path, restored.raw_path, restored.query_string) == (decoded, raw, query)


@pytest.mark.parametrize('target,decoded,raw,query', [
    ('//group/review?q=ok', '//group/review', b'//group/review', b'q=ok'),
    ('///group/review', '///group/review', b'///group/review', b''),
    ('/', '/', b'/', b''), ('//', '//', b'//', b''), ('///', '///', b'///', b''),
    ('/a%252Fb?x=%2F', '/a%2Fb', b'/a%252Fb', b'x=%2F'),
    ('/a%3Fb%23c?x=1', '/a?b#c', b'/a%3Fb%23c', b'x=1'),
    ('/p?x=1#ignored?y=2', '/p', b'/p', b'x=1'),
    ('/p#ignored?x=1', '/p', b'/p', b''),
])
def test_origin_components(target, decoded, raw, query):
    h1, h2 = _connections(target)
    for conn in (h1, h2):
        _assert_components(conn, decoded, raw, query)
    assert h1.headers.get(b'host') == b'localhost:8000'
    assert h2.headers.get(b'host') == b'example.com'
    assert h1.server == ('localhost', 8000)
    assert h2.server is None


@pytest.mark.parametrize('authority,server', [
    ('review.invalid', ('review.invalid', 80)),
    ('review.invalid:8080', ('review.invalid', 8080)),
    ('[::1]:8080', ('::1', 8080)),
])
@pytest.mark.parametrize('suffix,query', [
    ('?q=ok', b'q=ok'), ('?next=/review?x=1', b'next=/review?x=1'),
    ('?', b''), ('', b''), ('#ignored/path?x=1', b''),
    ('?next=http://other.invalid/p#ignored', b'next=http://other.invalid/p'),
])
def test_absolute_empty_path(authority, server, suffix, query):
    conn = object.__new__(HTTP1Actor)._parse(_http_request(
        path='http://' + authority + suffix, headers={'Host': 'conflict.invalid'}))
    _assert_components(conn, '/', b'/', query)
    assert conn.headers.get(b'host') == authority.encode()
    assert conn.server == server


@given(
    slashes=st.integers(min_value=1, max_value=5),
    segment=st.sampled_from([
        ('a;b+c', 'a;b+c'), ('a%2Fb', 'a/b'), ('a%252Fb', 'a%2Fb'),
        ('a%3Fb', 'a?b'), ('a%23b', 'a#b'), ('%ZZ', '%ZZ'),
        ('caf%C3%A9', 'café'), ('%C3', '�'),
    ]),
    query=st.sampled_from(['', 'x=%2F', 'next=/p?q=1', 'next=http://host/p']),
    query_present=st.booleans(),
)
def test_generated_origin_components(slashes, segment, query, query_present):
    raw = '/' * slashes + segment[0]
    target = raw + ('?' + query if query_present else '')
    for conn in _connections(target):
        _assert_components(conn, '/' * slashes + segment[1], raw.encode(),
                           query.encode() if query_present else b'')
