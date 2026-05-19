"""HTTP/1.1 request-smuggling vectors (RFC 9112 §11.2 security considerations).

The same family of bugs has shipped in nearly every HTTP server at some
point.  Each test below mirrors a named CVE class.  The framework that
went into the framing-validation commits should already block all of
them — these tests pin those guarantees so a future refactor can't
silently regress.

For a detailed taxonomy see PortSwigger Research, "HTTP Desync Attacks"
(2019).  Brief naming convention:

* CL.CL — two Content-Length headers (different values)
* CL.TE — Content-Length + Transfer-Encoding; receiver-A uses CL,
  receiver-B uses TE
* TE.CL — same combination but receiver-A uses TE, B uses CL
* TE.TE — Transfer-Encoding obfuscated so different parsers see it
  differently (e.g. capitalisation, junk parameters, duplicate header)
"""
import pytest

from .conftest import send_raw


@pytest.mark.integration
class TestCLCL:
    """Two Content-Length headers; safe receiver MUST reject."""

    def test_two_different_cl_rejected(self, h1_app):
        r = send_raw('127.0.0.1', h1_app.port,
                     b'POST /echo HTTP/1.1\r\n'
                     b'Host: localhost\r\n'
                     b'Content-Length: 8\r\n'
                     b'Content-Length: 0\r\n\r\n'
                     b'SMUGGLED')
        assert r.status != 200


@pytest.mark.integration
class TestCLTE:
    """Content-Length + Transfer-Encoding present — RFC 9112 §6.1.

    The smuggling attack: front-end uses CL=N, backend uses TE=chunked.
    The two endpoints disagree on where the request ends, leaving bytes
    in the backend's input buffer that get parsed as a new request.
    """

    def test_cl_followed_by_te_rejected(self, h1_app):
        r = send_raw('127.0.0.1', h1_app.port,
                     b'POST /echo HTTP/1.1\r\n'
                     b'Host: localhost\r\n'
                     b'Content-Length: 13\r\n'
                     b'Transfer-Encoding: chunked\r\n\r\n'
                     b'0\r\n\r\n'
                     b'SMUGGLED')
        assert r.status != 200

    def test_te_followed_by_cl_rejected(self, h1_app):
        """Header order MUST NOT change the verdict."""
        r = send_raw('127.0.0.1', h1_app.port,
                     b'POST /echo HTTP/1.1\r\n'
                     b'Host: localhost\r\n'
                     b'Transfer-Encoding: chunked\r\n'
                     b'Content-Length: 13\r\n\r\n'
                     b'0\r\n\r\n'
                     b'SMUGGLED')
        assert r.status != 200


@pytest.mark.integration
class TestTEObfuscation:
    """TE.TE — try to slip a Transfer-Encoding past one parser of the chain.

    The attack vector: encode the TE header in a way one parser sees and
    the other doesn't.  Any of these should produce a rejection OR be
    treated as a normal ``Transfer-Encoding: chunked`` (i.e. NEVER
    silently fall back to Content-Length).
    """

    def test_te_with_unknown_coding_rejected(self, h1_app):
        """``Transfer-Encoding: xchunked`` is an unknown coding — must NOT
        be silently demoted to "no TE" and fall back to Content-Length."""
        r = send_raw('127.0.0.1', h1_app.port,
                     b'POST /echo HTTP/1.1\r\n'
                     b'Host: localhost\r\n'
                     b'Content-Length: 5\r\n'
                     b'Transfer-Encoding: xchunked\r\n\r\n'
                     b'GET /smuggled HTTP/1.1\r\n\r\n')
        assert r.status != 200

    def test_te_with_extra_spaces_in_value_rejected_or_chunked(self, h1_app):
        """``Transfer-Encoding:  chunked`` (with extra OWS in the value)
        must either be treated as chunked or rejected — NEVER as
        "unknown TE, fall back to CL"."""
        r = send_raw('127.0.0.1', h1_app.port,
                     b'POST /echo HTTP/1.1\r\n'
                     b'Host: localhost\r\n'
                     b'Content-Length: 5\r\n'
                     b'Transfer-Encoding:  chunked\r\n\r\n'  # extra space
                     b'0\r\n\r\nSMUGGLED')
        # CL+TE → must reject
        assert r.status != 200

    def test_te_with_tab_indented_value_rejected(self, h1_app):
        """``Transfer-Encoding:\\tchunked`` — leading HTAB in value should
        still parse as chunked, then be rejected because CL is also set."""
        r = send_raw('127.0.0.1', h1_app.port,
                     b'POST /echo HTTP/1.1\r\n'
                     b'Host: localhost\r\n'
                     b'Content-Length: 5\r\n'
                     b'Transfer-Encoding:\tchunked\r\n\r\n'
                     b'0\r\n\r\nSMUGGLED')
        assert r.status != 200

    def test_duplicate_te_rejected(self, h1_app):
        """Two TE headers — even if both say chunked, the receiver SHOULD
        reject (RFC 9112 §6.1 says servers MUST close the connection
        after responding when multiple TE headers disagree about
        framing).  With CL also present, always reject."""
        r = send_raw('127.0.0.1', h1_app.port,
                     b'POST /echo HTTP/1.1\r\n'
                     b'Host: localhost\r\n'
                     b'Content-Length: 5\r\n'
                     b'Transfer-Encoding: chunked\r\n'
                     b'Transfer-Encoding: chunked\r\n\r\n'
                     b'0\r\n\r\nSMUGGLED')
        assert r.status != 200


@pytest.mark.integration
class TestSpaceBeforeColon:
    """`Content-Length : 5` — some legacy parsers strip the space and
    treat as CL; others see an unknown header and fall back.  Either way,
    the discrepancy is exploitable."""

    def test_cl_with_space_before_colon_rejected(self, h1_app):
        r = send_raw('127.0.0.1', h1_app.port,
                     b'POST /echo HTTP/1.1\r\n'
                     b'Host: localhost\r\n'
                     b'Content-Length : 5\r\n\r\n'
                     b'hello')
        assert r.status != 200

    def test_te_with_space_before_colon_rejected(self, h1_app):
        r = send_raw('127.0.0.1', h1_app.port,
                     b'POST /echo HTTP/1.1\r\n'
                     b'Host: localhost\r\n'
                     b'Transfer-Encoding : chunked\r\n\r\n'
                     b'0\r\n\r\n')
        assert r.status != 200


@pytest.mark.integration
class TestEmbeddedCRLF:
    """A CRLF inside a header value would let a smuggled request appear
    as a sibling header on the front-end and as a fresh request to the
    backend."""

    def test_lf_only_terminator_in_request_line_rejected(self, h1_app):
        """LF.CR.LF de-sync — front-end sees one request, backend sees two."""
        r = send_raw('127.0.0.1', h1_app.port,
                     b'GET / HTTP/1.1\nHost: localhost\n\n'
                     b'GET /admin HTTP/1.1\r\nHost: localhost\r\n\r\n')
        assert r.status != 200, (
            f'bare-LF request must be rejected; got {r.status}')

    def test_lf_only_terminator_in_headers_rejected(self, h1_app):
        """Bare LF between headers — same de-sync class."""
        r = send_raw('127.0.0.1', h1_app.port,
                     b'GET / HTTP/1.1\r\n'
                     b'Host: localhost\n'   # bare LF
                     b'X-Other: x\r\n\r\n')
        # Either reject (preferred) or process as a single legitimate
        # request — but NEVER 200-OK with stray bytes left in the buffer.
        # We accept either status code; the smuggle's "leftover" bytes
        # are what matters and the buffer is flushed by us calling close.
        assert r.status != 200
