"""RFC 10008 — The HTTP QUERY method, over HTTP/1.1 wire bytes.

QUERY is safe + idempotent + cacheable with a request body (the first new
standard method since PATCH).  The fixture app registers ``QUERY /query``
as a body echo, so these tests pin:

* §2 — a QUERY request with a body dispatches and the handler sees the body;
* RFC 9110 §9.1 — QUERY is a case-sensitive token (``query`` must not match);
* RFC 9110 §15.5.6 — other methods on a QUERY-only path draw 405 with an
  ``Allow`` header advertising QUERY.

Note: RFC 10008's cache-key rules (§4.2) bind *caches*, not origin
servers — BlackBull ships no response cache, so nothing here tests them.
"""
import pytest

from .conftest import send_raw


@pytest.mark.integration
class TestQueryDispatch:
    """QUERY requests route and carry a request body (RFC 10008 §2)."""

    def test_lowercase_query_does_not_dispatch(self, h1_app):
        """RFC 9110 §9.1 — methods are case-sensitive tokens."""
        r = send_raw('127.0.0.1', h1_app.port,
                     b'query /query HTTP/1.1\r\n'
                     b'Host: localhost\r\n'
                     b'Content-Length: 0\r\n\r\n')
        assert r.status != 200


@pytest.mark.parametrize('host,req,expected_body', [
    pytest.param(
        'localhost',
        b'POST /echo HTTP/1.1\r\nHost: x\r\n'
        b'Content-Length: 5\r\n\r\nhello', b'hello',
        id='canonical-content-length'),
    pytest.param(
        '127.0.0.1',
        b'QUERY /query HTTP/1.1\r\n'
        b'Host: localhost\r\n'
        b'Content-Type: text/plain\r\n'
        b'Content-Length: 8\r\n\r\n'
        b'select *', b'select *',
        id='query-with-body', marks=pytest.mark.integration),
    pytest.param(
        '127.0.0.1',
        b'QUERY /query HTTP/1.1\r\n'
        b'Host: localhost\r\n'
        b'Content-Length: 0\r\n\r\n', b'',
        id='query-empty-body', marks=pytest.mark.integration),
    pytest.param(
        '127.0.0.1',
        b'POST /echo HTTP/1.1\r\n'
        b'Host: localhost\r\n'
        b'Content-Length: 0\r\n\r\n', b'',
        id='post-zero-length', marks=pytest.mark.integration),
    pytest.param(
        '127.0.0.1',
        b'POST /echo HTTP/1.1\r\n'
        b'Host: localhost\r\n'
        b'Content-Length: 5\r\n\r\n'
        b'hello', b'hello',
        id='well-formed-content-length', marks=pytest.mark.integration),
    pytest.param(
        '127.0.0.1',
        b'POST /echo HTTP/1.1\r\n'
        b'Host: localhost\r\n'
        b'Content-Length: 0\r\n\r\n', b'',
        id='zero-content-length', marks=pytest.mark.integration),
])
def test_query_with_body_echoes(h1_app, host, req, expected_body):
    """Well-formed request bodies are accepted and echoed (RFC 10008 §2,
    RFC 9112 §6).  Module-level with per-case integration marks so the one
    unmarked case keeps running in the default suite."""
    r = send_raw(host, h1_app.port, req)
    assert r.status == 200
    assert r.body == expected_body


@pytest.mark.integration
class TestQueryIn405Allow:
    """405 responses for a QUERY-registered path advertise QUERY in Allow."""

    def test_post_to_query_only_path_is_405(self, h1_app):
        r = send_raw('127.0.0.1', h1_app.port,
                     b'POST /query HTTP/1.1\r\n'
                     b'Host: localhost\r\n'
                     b'Content-Length: 0\r\n\r\n')
        assert r.status == 405

    def test_allow_header_contains_query(self, h1_app):
        r = send_raw('127.0.0.1', h1_app.port,
                     b'GET /query HTTP/1.1\r\n'
                     b'Host: localhost\r\n\r\n')
        assert r.status == 405
        allow = r.header(b'allow')
        assert allow is not None, '405 must carry an Allow header'
        tokens = {t.strip() for t in allow.split(b',')}
        assert b'QUERY' in tokens, f'Allow must advertise QUERY; got {allow!r}'

    def test_query_on_get_only_path_is_405_without_query_in_allow(self, h1_app):
        r = send_raw('127.0.0.1', h1_app.port,
                     b'QUERY / HTTP/1.1\r\n'
                     b'Host: localhost\r\n'
                     b'Content-Length: 0\r\n\r\n')
        assert r.status == 405
        allow = r.header(b'allow') or b''
        tokens = {t.strip() for t in allow.split(b',')}
        assert b'QUERY' not in tokens


@pytest.mark.integration
class TestAcceptQueryOnWire:
    """RFC 10008 §2.2/§3 — Accept-Query header + Content-Type enforcement
    on a live QUERY route declared with ``accept_query=[...]``."""

    def test_accept_query_header_on_success(self, h1_app):
        r = send_raw('127.0.0.1', h1_app.port,
                     b'QUERY /query-typed HTTP/1.1\r\n'
                     b'Host: localhost\r\n'
                     b'Content-Type: application/sql\r\n'
                     b'Content-Length: 8\r\n\r\n'
                     b'select *')
        assert r.status == 200
        assert r.body == b'select *'
        assert r.header(b'accept-query') == b'application/sql, text/plain'

    def test_missing_content_type_is_400(self, h1_app):
        r = send_raw('127.0.0.1', h1_app.port,
                     b'QUERY /query-typed HTTP/1.1\r\n'
                     b'Host: localhost\r\n'
                     b'Content-Length: 2\r\n\r\n'
                     b'{}')
        assert r.status == 400

    def test_unsupported_media_type_is_415_with_accept_query(self, h1_app):
        r = send_raw('127.0.0.1', h1_app.port,
                     b'QUERY /query-typed HTTP/1.1\r\n'
                     b'Host: localhost\r\n'
                     b'Content-Type: application/json\r\n'
                     b'Content-Length: 2\r\n\r\n'
                     b'{}')
        assert r.status == 415
        assert r.header(b'accept-query') == b'application/sql, text/plain'
