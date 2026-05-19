"""RFC 9112 §3, §4, §5 — Message Format conformance for HTTP/1.1.

Covers the wire-level syntax rules a conforming HTTP/1.1 receiver must
enforce on incoming requests:

* §3 Message Format  — request-line CRLF *(field-line CRLF) CRLF body
* §4 Request Line    — method SP request-target SP HTTP-version
* §5 Field Syntax    — field-name OWS ':' OWS field-value OWS, no line folding

Anything that violates these rules is supposed to be rejected with
400 Bad Request (or the connection dropped).  The point of these tests is
to make sure we don't silently accept malformed input — that's where
HTTP-level smuggling vectors hide.
"""
import pytest

from .conftest import send_raw


@pytest.mark.integration
class TestRequestLine:
    """RFC 9112 §3.1 — request-line = method SP request-target SP HTTP-version CRLF"""

    def test_well_formed_request_accepted(self, h1_app):
        r = send_raw('127.0.0.1', h1_app.port,
                     b'GET / HTTP/1.1\r\nHost: localhost\r\n\r\n')
        assert r.status == 200, f'baseline GET / must succeed; got {r.status}'
        assert r.body == b'ok'

    def test_missing_http_version_rejected(self, h1_app):
        """§4: HTTP-version is required.  Either reject or close the connection."""
        r = send_raw('127.0.0.1', h1_app.port,
                     b'GET /\r\nHost: localhost\r\n\r\n')
        assert r.status != 200, (
            f'request line without HTTP-version must not yield 200; got {r.status}')

    def test_missing_request_target_rejected(self, h1_app):
        """§4: request-target is required between method and HTTP-version."""
        r = send_raw('127.0.0.1', h1_app.port,
                     b'GET HTTP/1.1\r\nHost: localhost\r\n\r\n')
        assert r.status != 200

    def test_bare_lf_in_request_line_rejected(self, h1_app):
        """§2.2: a recipient MUST NOT interpret a bare LF as a CRLF.

        Accepting LF-only line terminators is a request-smuggling vector
        (the LF.CR.LF de-sync class).
        """
        r = send_raw('127.0.0.1', h1_app.port,
                     b'GET / HTTP/1.1\nHost: localhost\n\n')
        assert r.status != 200, (
            f'bare LF without CR MUST be rejected; got {r.status}')

    def test_lowercase_method_accepted_as_unknown(self, h1_app):
        """§9.1 (RFC 9110): methods are case-sensitive; ``get`` is not ``GET``.

        BlackBull's router won't match an arbitrary lowercase method and
        should return 405 / 404 / similar — never 200 against the GET handler.
        """
        r = send_raw('127.0.0.1', h1_app.port,
                     b'get / HTTP/1.1\r\nHost: localhost\r\n\r\n')
        # Accept any non-200 response (the parser may also outright reject).
        assert r.status != 200

    def test_request_with_extra_spaces_rejected(self, h1_app):
        """§3: exactly one SP between method/target/version (no run-of-spaces)."""
        r = send_raw('127.0.0.1', h1_app.port,
                     b'GET  /  HTTP/1.1\r\nHost: localhost\r\n\r\n')
        assert r.status != 200

    def test_request_target_with_tab_rejected(self, h1_app):
        """Tab between method and request-target is not SP."""
        r = send_raw('127.0.0.1', h1_app.port,
                     b'GET\t/\tHTTP/1.1\r\nHost: localhost\r\n\r\n')
        assert r.status != 200

    def test_http_0_9_request_rejected(self, h1_app):
        """An HTTP/0.9-style request (no version, no headers) MUST NOT be
        treated as HTTP/1.1.  RFC 9112 §2: only HTTP/1.x is in scope here."""
        r = send_raw('127.0.0.1', h1_app.port, b'GET /\r\n\r\n')
        assert r.status != 200


@pytest.mark.integration
class TestStartLineLeadingCRLF:
    """RFC 9112 §2.2 — recipients MAY skip empty CRLFs before a request.

    Many HTTP/1.0 clients (and some HTTP/1.1) emit a stray CRLF before each
    request.  The standard says we MAY tolerate it; we do not have to.  This
    test just asserts that whatever we do (skip or reject), we don't crash.
    """

    def test_leading_crlf_does_not_crash(self, h1_app):
        r = send_raw('127.0.0.1', h1_app.port,
                     b'\r\nGET / HTTP/1.1\r\nHost: localhost\r\n\r\n')
        # Either tolerated (200) or rejected (4xx).  A 5xx or hang would be a bug.
        assert r.status is None or 200 <= r.status < 500


@pytest.mark.integration
class TestFieldSyntax:
    """RFC 9112 §5 — field-line = field-name ":" OWS field-value OWS"""

    def test_header_without_colon_rejected(self, h1_app):
        r = send_raw('127.0.0.1', h1_app.port,
                     b'GET / HTTP/1.1\r\n'
                     b'Host: localhost\r\n'
                     b'NotAHeader\r\n'
                     b'\r\n')
        assert r.status != 200

    def test_whitespace_before_colon_rejected(self, h1_app):
        """§5.1: 'No whitespace is allowed between the field-name and colon'."""
        r = send_raw('127.0.0.1', h1_app.port,
                     b'GET / HTTP/1.1\r\n'
                     b'Host : localhost\r\n'  # space before colon
                     b'\r\n')
        assert r.status != 200, (
            'space before colon (smuggling vector) MUST be rejected')

    def test_obsolete_line_folding_rejected(self, h1_app):
        """§5.2: 'A server that receives an obs-fold in a request message...
        MUST either reject the message... or replace each obs-fold with one
        or more SP octets'.  BlackBull chooses to reject."""
        r = send_raw('127.0.0.1', h1_app.port,
                     b'GET / HTTP/1.1\r\n'
                     b'Host: localhost\r\n'
                     b'X-Folded: line1\r\n'
                     b' line2\r\n'             # obs-fold continuation
                     b'\r\n')
        assert r.status != 200

    def test_tab_in_field_value_accepted(self, h1_app):
        """§5: field-value MAY contain HTAB (0x09)."""
        r = send_raw('127.0.0.1', h1_app.port,
                     b'GET / HTTP/1.1\r\n'
                     b'Host: localhost\r\n'
                     b'X-Has-Tab: a\tb\r\n'
                     b'\r\n')
        assert r.status == 200

    def test_leading_optional_whitespace_in_value_stripped(self, h1_app):
        """OWS surrounding field-value is not part of the value."""
        r = send_raw('127.0.0.1', h1_app.port,
                     b'GET / HTTP/1.1\r\n'
                     b'Host:    localhost   \r\n'   # OWS on both sides
                     b'\r\n')
        assert r.status == 200

    def test_empty_field_value_accepted(self, h1_app):
        """§5: field-value MAY be empty (e.g. ``Header:\r\n``)."""
        r = send_raw('127.0.0.1', h1_app.port,
                     b'GET / HTTP/1.1\r\n'
                     b'Host: localhost\r\n'
                     b'X-Empty:\r\n'
                     b'\r\n')
        assert r.status == 200

    def test_nul_in_field_value_rejected(self, h1_app):
        """§5: NUL is not allowed in field-value (smuggling / log-injection)."""
        r = send_raw('127.0.0.1', h1_app.port,
                     b'GET / HTTP/1.1\r\n'
                     b'Host: localhost\r\n'
                     b'X-Bad: a\x00b\r\n'
                     b'\r\n')
        assert r.status != 200


@pytest.mark.integration
class TestMessageBoundary:
    """RFC 9112 §3: end-of-headers is CRLF CRLF.  Anything else is malformed."""

    def test_missing_blank_line_after_headers_times_out(self, h1_app):
        """Without the terminating CRLF CRLF, the server is still waiting for
        more headers.  When we shut down the write half, behaviour can be
        either 400 or connection close — what matters is we don't 200."""
        r = send_raw('127.0.0.1', h1_app.port,
                     b'GET / HTTP/1.1\r\nHost: localhost\r\n')
        assert r.status != 200

    def test_extra_blank_lines_before_body_are_part_of_body(self, h1_app):
        """If we have Content-Length: N, the bytes after CRLF CRLF are body
        for exactly N octets — extra blank lines must not be re-parsed."""
        r = send_raw('127.0.0.1', h1_app.port,
                     b'POST /echo HTTP/1.1\r\n'
                     b'Host: localhost\r\n'
                     b'Content-Length: 5\r\n'
                     b'\r\n'
                     b'\r\n\r\nx')
        assert r.status == 200
        assert r.body == b'\r\n\r\nx'
