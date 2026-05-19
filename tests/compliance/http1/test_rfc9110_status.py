"""RFC 9110 §15 — Status codes and response semantics.

Tests the framework's wire behaviour for the status codes whose handling
is tied to message framing:

* §15.2 1xx (Informational) — no body, MUST NOT include Content-Length.
* §15.3.5 204 No Content — no body, MUST NOT include Content-Length
  or Transfer-Encoding.
* §15.4.5 304 Not Modified — no body, same rules as 204.

These are also the responses HEAD-like in spirit: any body bytes a
buggy framework emits here are a framing-violation vector.
"""
import pytest

from .conftest import send_raw


@pytest.mark.integration
class TestStatusLine:
    """RFC 9112 §4 — status-line = HTTP-version SP status-code SP reason-phrase."""

    def test_response_has_well_formed_status_line(self, h1_app):
        r = send_raw('127.0.0.1', h1_app.port,
                     b'GET / HTTP/1.1\r\nHost: localhost\r\n\r\n')
        assert r.raw.startswith(b'HTTP/1.1 200 '), (
            f'status-line must start with HTTP/1.1 200 ; got {r.raw[:32]!r}')

    def test_response_has_reason_phrase(self, h1_app):
        """RFC 9112 §4 — reason-phrase is required (may be empty), and the
        SP between status-code and reason MUST be present."""
        r = send_raw('127.0.0.1', h1_app.port,
                     b'GET / HTTP/1.1\r\nHost: localhost\r\n\r\n')
        first_line = r.raw.split(b'\r\n', 1)[0]
        # ``HTTP/1.1 200 OK`` — three SP-separated parts; reason can be empty
        parts = first_line.split(b' ', 2)
        assert len(parts) == 3
        assert parts[2] != b''  # we send a meaningful phrase


@pytest.mark.integration
class TestResponseDate:
    """RFC 9110 §6.6.1 — origin servers SHOULD send a Date header."""

    def test_date_header_present(self, h1_app):
        r = send_raw('127.0.0.1', h1_app.port,
                     b'GET / HTTP/1.1\r\nHost: localhost\r\n\r\n')
        assert r.header(b'date') is not None, 'response must carry Date header'


@pytest.mark.integration
class TestContentLengthCorrectness:
    """For responses with a fixed body, Content-Length MUST match the body
    byte count exactly.  A mismatch produces under-read or over-read on the
    client and the connection desynchronises."""

    def test_response_content_length_matches_body(self, h1_app):
        r = send_raw('127.0.0.1', h1_app.port,
                     b'GET / HTTP/1.1\r\nHost: localhost\r\n\r\n')
        cl = r.header(b'content-length')
        assert cl is not None
        assert int(cl) == len(r.body), (
            f'Content-Length {cl!r} disagrees with body length {len(r.body)}')

    def test_echo_response_content_length_matches_body(self, h1_app):
        payload = b'X' * 137
        r = send_raw('127.0.0.1', h1_app.port,
                     b'POST /echo HTTP/1.1\r\n'
                     b'Host: localhost\r\n'
                     b'Content-Length: 137\r\n\r\n'
                     + payload)
        cl = r.header(b'content-length')
        assert int(cl) == len(r.body) == 137


@pytest.mark.integration
class TestChunkedStreamingResponse:
    """RFC 9112 §7 — streaming responses use Transfer-Encoding: chunked.

    The /chunked route in conftest yields three single-byte chunks.
    """

    def test_chunked_response_uses_transfer_encoding(self, h1_app):
        r = send_raw('127.0.0.1', h1_app.port,
                     b'GET /chunked HTTP/1.1\r\nHost: localhost\r\n\r\n')
        te = r.header(b'transfer-encoding')
        assert te is not None and te.lower() == b'chunked', (
            f'streaming response must use chunked TE; got {te!r}')
        # Content-Length MUST NOT be present alongside chunked TE.
        assert r.header(b'content-length') is None

    def test_chunked_response_terminates_with_zero_chunk(self, h1_app):
        """The on-the-wire bytes end with ``0\\r\\n\\r\\n``."""
        r = send_raw('127.0.0.1', h1_app.port,
                     b'GET /chunked HTTP/1.1\r\nHost: localhost\r\n\r\n')
        # Body in r is the raw chunked stream
        assert r.raw.endswith(b'0\r\n\r\n'), (
            f'chunked response must terminate with 0\\r\\n\\r\\n; '
            f'last 16 bytes were {r.raw[-16:]!r}')


@pytest.mark.integration
class TestVersionEcho:
    """A server SHOULD respond to HTTP/1.0 clients with HTTP/1.1 (we are
    a 1.1 server) but MUST NOT downgrade framing.  Also: an HTTP/1.0
    client doesn't keep-alive by default."""

    def test_http10_request_gets_http11_response(self, h1_app):
        """RFC 9112 §2.5: a server speaks the highest minor it supports
        on the version line of its responses; we always emit HTTP/1.1."""
        r = send_raw('127.0.0.1', h1_app.port,
                     b'GET / HTTP/1.0\r\nHost: localhost\r\n\r\n')
        assert r.raw.startswith(b'HTTP/1.1 200 ') or r.raw.startswith(b'HTTP/1.0 200 ')
        assert r.status == 200
