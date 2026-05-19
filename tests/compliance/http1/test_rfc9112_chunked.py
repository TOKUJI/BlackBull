"""RFC 9112 §7 — Chunked Transfer Coding conformance.

Each chunk on the wire is::

    chunk          = chunk-size [ chunk-ext ] CRLF chunk-data CRLF
    chunk-size     = 1*HEXDIG
    chunk-ext      = *( ";" chunk-ext-name [ "=" chunk-ext-val ] )
    last-chunk     = 1*("0") [ chunk-ext ] CRLF
    trailer-part   = *( header-field CRLF )

Areas that commonly hide bugs:

* hex-only chunk-size (lower or upper case)
* chunk-ext after ``;`` must be ignored, not parsed as part of the size
* malformed chunk-size (non-hex, empty, signed) must reject
* chunk-data length must equal chunk-size
* trailer-part may follow last-chunk; must not be merged into the next
  request when keep-alive is in use
"""
import pytest

from .conftest import send_raw


@pytest.mark.integration
class TestChunkedHappyPath:
    def test_basic_chunked_body_round_trips(self, h1_app):
        r = send_raw('127.0.0.1', h1_app.port,
                     b'POST /echo HTTP/1.1\r\n'
                     b'Host: localhost\r\n'
                     b'Transfer-Encoding: chunked\r\n\r\n'
                     b'5\r\nhello\r\n'
                     b'6\r\n world\r\n'
                     b'0\r\n\r\n')
        assert r.status == 200
        assert r.body == b'hello world'

    def test_empty_chunked_body(self, h1_app):
        """0\\r\\n\\r\\n is a valid (empty) chunked body."""
        r = send_raw('127.0.0.1', h1_app.port,
                     b'POST /echo HTTP/1.1\r\n'
                     b'Host: localhost\r\n'
                     b'Transfer-Encoding: chunked\r\n\r\n'
                     b'0\r\n\r\n')
        assert r.status == 200
        assert r.body == b''

    def test_chunk_size_uppercase_hex(self, h1_app):
        """Chunk-size hex digits MAY be either case (§7.1.1)."""
        r = send_raw('127.0.0.1', h1_app.port,
                     b'POST /echo HTTP/1.1\r\n'
                     b'Host: localhost\r\n'
                     b'Transfer-Encoding: chunked\r\n\r\n'
                     b'A\r\n0123456789\r\n'
                     b'0\r\n\r\n')
        assert r.status == 200
        assert r.body == b'0123456789'

    def test_chunk_ext_ignored(self, h1_app):
        """``5;foo=bar\\r\\nhello\\r\\n0\\r\\n\\r\\n`` — chunk-ext must be ignored, not affect size."""
        r = send_raw('127.0.0.1', h1_app.port,
                     b'POST /echo HTTP/1.1\r\n'
                     b'Host: localhost\r\n'
                     b'Transfer-Encoding: chunked\r\n\r\n'
                     b'5;foo=bar\r\nhello\r\n'
                     b'0\r\n\r\n')
        assert r.status == 200
        assert r.body == b'hello'

    def test_last_chunk_with_extension(self, h1_app):
        r = send_raw('127.0.0.1', h1_app.port,
                     b'POST /echo HTTP/1.1\r\n'
                     b'Host: localhost\r\n'
                     b'Transfer-Encoding: chunked\r\n\r\n'
                     b'0;name=value\r\n\r\n')
        assert r.status == 200
        assert r.body == b''


@pytest.mark.integration
class TestChunkedTrailers:
    def test_trailer_after_last_chunk_accepted(self, h1_app):
        """Trailers MAY follow the 0\\r\\n line and end with CRLF CRLF."""
        r = send_raw('127.0.0.1', h1_app.port,
                     b'POST /echo HTTP/1.1\r\n'
                     b'Host: localhost\r\n'
                     b'Transfer-Encoding: chunked\r\n'
                     b'Trailer: X-Checksum\r\n\r\n'
                     b'5\r\nhello\r\n'
                     b'0\r\n'
                     b'X-Checksum: abc123\r\n\r\n')
        assert r.status == 200
        assert r.body == b'hello'


@pytest.mark.integration
class TestChunkedMalformed:
    """§7.1 — malformed chunked bodies must be rejected, not silently truncated."""

    def test_non_hex_chunk_size_rejected(self, h1_app):
        r = send_raw('127.0.0.1', h1_app.port,
                     b'POST /echo HTTP/1.1\r\n'
                     b'Host: localhost\r\n'
                     b'Transfer-Encoding: chunked\r\n\r\n'
                     b'XYZ\r\nhello\r\n'
                     b'0\r\n\r\n')
        assert r.status != 200, (
            f'non-hex chunk-size must be rejected; got {r.status}')

    def test_signed_chunk_size_rejected(self, h1_app):
        """``-5\\r\\nhello\\r\\n`` — negative size has no meaning."""
        r = send_raw('127.0.0.1', h1_app.port,
                     b'POST /echo HTTP/1.1\r\n'
                     b'Host: localhost\r\n'
                     b'Transfer-Encoding: chunked\r\n\r\n'
                     b'-5\r\nhello\r\n'
                     b'0\r\n\r\n')
        assert r.status != 200

    def test_empty_chunk_size_rejected(self, h1_app):
        """A line consisting only of CRLF where chunk-size should be."""
        r = send_raw('127.0.0.1', h1_app.port,
                     b'POST /echo HTTP/1.1\r\n'
                     b'Host: localhost\r\n'
                     b'Transfer-Encoding: chunked\r\n\r\n'
                     b'\r\nhello\r\n'
                     b'0\r\n\r\n')
        assert r.status != 200
