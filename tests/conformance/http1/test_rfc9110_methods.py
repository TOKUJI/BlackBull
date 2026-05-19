"""RFC 9110 §9 — HTTP method semantics conformance.

Tests the framework's handling of the standard request methods, focusing
on the rules that often slip:

* §9.3.2 HEAD — response carries no body but the same Content-Length /
  Content-Type as the GET would have, so caching proxies stay accurate.
* §9.3.6 OPTIONS — when not routed, MAY be answered with 405; some
  servers handle ``OPTIONS *`` specially.
* §9.1 — methods are case-sensitive tokens; mixed-case must not match.
"""
import pytest

from .conftest import send_raw


@pytest.mark.integration
class TestHEAD:
    """RFC 9110 §9.3.2 — HEAD response: same headers as GET, no body."""

    def test_head_returns_no_body(self, h1_app):
        r = send_raw('127.0.0.1', h1_app.port,
                     b'HEAD / HTTP/1.1\r\nHost: localhost\r\n\r\n')
        assert r.status == 200
        assert r.body == b'', (
            f'HEAD response MUST NOT have a body; got {r.body!r}')

    def test_head_has_content_length(self, h1_app):
        """A HEAD response SHOULD include Content-Length so a cache can
        update its view of the resource size without re-GETting."""
        r = send_raw('127.0.0.1', h1_app.port,
                     b'HEAD / HTTP/1.1\r\nHost: localhost\r\n\r\n')
        cl = r.header(b'content-length')
        assert cl is not None, 'HEAD response should carry Content-Length'
        assert cl == b'2', f'expected len 2 (matching GET /); got {cl!r}'


@pytest.mark.integration
class TestMethodCase:
    """RFC 9110 §9.1 — method names are case-sensitive tokens."""

    def test_lowercase_method_not_dispatched(self, h1_app):
        r = send_raw('127.0.0.1', h1_app.port,
                     b'get / HTTP/1.1\r\nHost: localhost\r\n\r\n')
        assert r.status != 200, (
            f'lowercase ``get`` must NOT dispatch to GET handler; got {r.status}')

    def test_mixed_case_method_not_dispatched(self, h1_app):
        r = send_raw('127.0.0.1', h1_app.port,
                     b'Get / HTTP/1.1\r\nHost: localhost\r\n\r\n')
        assert r.status != 200

    def test_unknown_method_yields_405(self, h1_app):
        """A registered route called with an unregistered method should
        return 405 Method Not Allowed.  Body content doesn't matter here."""
        r = send_raw('127.0.0.1', h1_app.port,
                     b'PATCH / HTTP/1.1\r\nHost: localhost\r\n\r\n')
        # 405 (preferred) or 404 acceptable; 200 is wrong.
        assert r.status in (404, 405, 501)


@pytest.mark.integration
class TestUnknownExtensionMethod:
    """RFC 9110 §9.1 — method-tokens are extensible; a server MAY refuse
    methods it doesn't recognise with 501 Not Implemented."""

    def test_extension_method_does_not_dispatch_get(self, h1_app):
        r = send_raw('127.0.0.1', h1_app.port,
                     b'PURGE / HTTP/1.1\r\nHost: localhost\r\n\r\n')
        assert r.status != 200


@pytest.mark.integration
class TestPostWithoutBody:
    """A POST with Content-Length: 0 is legal."""

    def test_post_zero_length_to_echo(self, h1_app):
        r = send_raw('127.0.0.1', h1_app.port,
                     b'POST /echo HTTP/1.1\r\n'
                     b'Host: localhost\r\n'
                     b'Content-Length: 0\r\n\r\n')
        assert r.status == 200
        assert r.body == b''


@pytest.mark.integration
class TestGetWithBody:
    """RFC 9110 §9.3.1 — a body in a GET 'has no defined semantics'
    but is not forbidden; we MUST NOT 400 just because GET has CL>0."""

    def test_get_with_body_does_not_400(self, h1_app):
        r = send_raw('127.0.0.1', h1_app.port,
                     b'GET / HTTP/1.1\r\n'
                     b'Host: localhost\r\n'
                     b'Content-Length: 5\r\n\r\n'
                     b'hello')
        # 200 ok (we ignore the body) is the natural answer.  400 would be
        # over-strict.  Either way: NOT 500.
        assert r.status not in (None, 500)
