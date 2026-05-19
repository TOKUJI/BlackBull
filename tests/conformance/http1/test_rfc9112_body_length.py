"""RFC 9112 §6 — Message Body Length conformance.

§6 describes how a receiver determines where a request body ends.  Getting
this wrong is the root cause of every HTTP request-smuggling class
(CL.TE, TE.CL, TE.TE, CL.CL).  The rules implementations get wrong most
often:

* §6.1 — Transfer-Encoding takes precedence over Content-Length.  If both
  are present, the message is anomalous; Content-Length MUST be ignored
  or the message rejected.  A safer policy is to reject (§11.1).
* §6.2 — Multiple Content-Length header fields are forbidden unless they
  all carry the same single value.  Different values → reject.
* §6.2 — Comma-separated Content-Length values like ``42, 42`` MAY be
  accepted only when every value is identical.
* §6.3 — Content-Length values MUST be a non-empty string of DIGITs.
  Whitespace, leading ``+``, hex, signed, scientific — all rejected.
"""
import pytest

from .conftest import send_raw


@pytest.mark.integration
class TestContentLengthValidation:
    """§6.2 — Content-Length value must be a non-negative integer."""

    def test_well_formed_content_length(self, h1_app):
        r = send_raw('127.0.0.1', h1_app.port,
                     b'POST /echo HTTP/1.1\r\n'
                     b'Host: localhost\r\n'
                     b'Content-Length: 5\r\n\r\n'
                     b'hello')
        assert r.status == 200
        assert r.body == b'hello'

    def test_zero_content_length_no_body(self, h1_app):
        r = send_raw('127.0.0.1', h1_app.port,
                     b'POST /echo HTTP/1.1\r\n'
                     b'Host: localhost\r\n'
                     b'Content-Length: 0\r\n\r\n')
        assert r.status == 200
        assert r.body == b''

    def test_content_length_with_leading_plus_rejected(self, h1_app):
        """``Content-Length: +5`` is not a DIGIT-only value (§6.3)."""
        r = send_raw('127.0.0.1', h1_app.port,
                     b'POST /echo HTTP/1.1\r\n'
                     b'Host: localhost\r\n'
                     b'Content-Length: +5\r\n\r\n'
                     b'hello')
        assert r.status != 200

    def test_content_length_with_minus_rejected(self, h1_app):
        r = send_raw('127.0.0.1', h1_app.port,
                     b'POST /echo HTTP/1.1\r\n'
                     b'Host: localhost\r\n'
                     b'Content-Length: -5\r\n\r\n'
                     b'hello')
        assert r.status != 200

    def test_content_length_hex_rejected(self, h1_app):
        r = send_raw('127.0.0.1', h1_app.port,
                     b'POST /echo HTTP/1.1\r\n'
                     b'Host: localhost\r\n'
                     b'Content-Length: 0xff\r\n\r\n')
        assert r.status != 200

    def test_content_length_with_trailing_garbage_rejected(self, h1_app):
        """Trailing non-digit bytes (``5x``) MUST be rejected — silently
        truncating to 5 would let an attacker smuggle the trailing bytes."""
        r = send_raw('127.0.0.1', h1_app.port,
                     b'POST /echo HTTP/1.1\r\n'
                     b'Host: localhost\r\n'
                     b'Content-Length: 5x\r\n\r\n'
                     b'hello')
        assert r.status != 200

    def test_empty_content_length_rejected(self, h1_app):
        r = send_raw('127.0.0.1', h1_app.port,
                     b'POST /echo HTTP/1.1\r\n'
                     b'Host: localhost\r\n'
                     b'Content-Length:\r\n\r\n')
        assert r.status != 200


@pytest.mark.integration
class TestDuplicateContentLength:
    """§6.2 — Multiple Content-Length headers MUST be consistent or rejected."""

    def test_duplicate_content_length_same_value_accepted_or_rejected(self, h1_app):
        """RFC 9112 §6.2 leaves the choice: either treat ``42, 42`` as 42 or
        reject.  We require the response not to be 200 OK with the wrong
        body length — that's the dangerous outcome."""
        r = send_raw('127.0.0.1', h1_app.port,
                     b'POST /echo HTTP/1.1\r\n'
                     b'Host: localhost\r\n'
                     b'Content-Length: 5\r\n'
                     b'Content-Length: 5\r\n\r\n'
                     b'hello')
        # Acceptable: either 200 with body 'hello' or 400.
        if r.status == 200:
            assert r.body == b'hello'
        else:
            assert r.status != 200

    def test_duplicate_content_length_different_values_rejected(self, h1_app):
        """Different values is the CL.CL request-smuggling vector — reject."""
        r = send_raw('127.0.0.1', h1_app.port,
                     b'POST /echo HTTP/1.1\r\n'
                     b'Host: localhost\r\n'
                     b'Content-Length: 5\r\n'
                     b'Content-Length: 6\r\n\r\n'
                     b'hello!')
        assert r.status != 200, (
            f'conflicting Content-Length headers MUST be rejected; got {r.status}')

    def test_comma_combined_inconsistent_content_length_rejected(self, h1_app):
        """``Content-Length: 5, 6`` — comma-combined inconsistent values."""
        r = send_raw('127.0.0.1', h1_app.port,
                     b'POST /echo HTTP/1.1\r\n'
                     b'Host: localhost\r\n'
                     b'Content-Length: 5, 6\r\n\r\n'
                     b'hello!')
        assert r.status != 200


@pytest.mark.integration
class TestTransferEncodingVsContentLength:
    """§6.1 — TE takes precedence; the combination is a smuggling vector."""

    def test_both_present_message_rejected_or_te_wins(self, h1_app):
        """RFC 9112 §6.1: 'a sender MUST NOT send a request containing
        Transfer-Encoding with a Content-Length' ... 'a server that
        receives a request message with both Content-Length and a
        Transfer-Encoding header field ... MUST close the connection
        after responding'.

        Safer policy: reject outright with 400.  Letting the request
        through with EITHER framing is an attack surface.
        """
        body = b'5\r\nhello\r\n0\r\n\r\n'
        r = send_raw('127.0.0.1', h1_app.port,
                     b'POST /echo HTTP/1.1\r\n'
                     b'Host: localhost\r\n'
                     b'Content-Length: 5\r\n'
                     b'Transfer-Encoding: chunked\r\n\r\n'
                     + body)
        assert r.status != 200, (
            f'CL+TE together MUST be rejected (smuggling vector); got {r.status}')


@pytest.mark.integration
class TestUnknownTransferCoding:
    """§7 — only known transfer codings may be applied to requests."""

    def test_unknown_transfer_encoding_rejected(self, h1_app):
        """An unknown coding (e.g. ``gzip,chunked,evil``) MUST be answered
        with 501 Not Implemented per §7.4."""
        r = send_raw('127.0.0.1', h1_app.port,
                     b'POST /echo HTTP/1.1\r\n'
                     b'Host: localhost\r\n'
                     b'Transfer-Encoding: chunked, evil\r\n\r\n'
                     b'0\r\n\r\n')
        assert r.status != 200

    def test_transfer_encoding_not_ending_in_chunked_rejected(self, h1_app):
        """§6.1: if a Transfer-Encoding list is present, the final coding
        applied to a request MUST be ``chunked`` — otherwise the message
        length cannot be determined."""
        r = send_raw('127.0.0.1', h1_app.port,
                     b'POST /echo HTTP/1.1\r\n'
                     b'Host: localhost\r\n'
                     b'Transfer-Encoding: gzip\r\n\r\n')
        assert r.status != 200
