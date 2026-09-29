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
    @pytest.mark.parametrize('chunk_tail,expected_body', [
        pytest.param(b'5\r\nhello\r\n'
                     b'6\r\n world\r\n'
                     b'0\r\n\r\n', b'hello world', id='basic-round-trip'),
        pytest.param(b'0;name=value\r\n\r\n', b'', id='last-chunk-extension'),
        pytest.param(b'5\r\nhello\r\n0\r\n\r\n', b'hello', id='valid-chunked-200'),
    ])
    def test_basic_chunked_body_round_trips(self, h1_app, chunk_tail, expected_body):
        """Well-formed chunked bodies are accepted and echoed."""
        r = send_raw('127.0.0.1', h1_app.port,
                     b'POST /echo HTTP/1.1\r\n'
                     b'Host: localhost\r\n'
                     b'Transfer-Encoding: chunked\r\n\r\n' + chunk_tail)
        assert r.status == 200
        assert r.body == expected_body

@pytest.mark.integration
class TestChunkedTrailers:
    @pytest.mark.parametrize('headers,tail,expected_body', [
        pytest.param(b'', b'0\r\n\r\n', b'',
                     id='empty-chunked-body'),
        pytest.param(b'', b'A\r\n0123456789\r\n0\r\n\r\n', b'0123456789',
                     id='uppercase-hex-size'),
        pytest.param(b'', b'5;foo=bar\r\nhello\r\n0\r\n\r\n', b'hello',
                     id='chunk-ext-ignored'),
        pytest.param(b'Trailer: X-Checksum\r\n',
                     b'5\r\nhello\r\n0\r\nX-Checksum: abc123\r\n\r\n', b'hello',
                     id='trailer-after-last-chunk'),
    ])
    def test_trailer_after_last_chunk_accepted(self, h1_app, headers, tail, expected_body):
        """Valid chunked encodings are accepted: empty body, uppercase hex
        size (§7.1.1), ignored chunk-ext, trailers after the 0 chunk."""
        r = send_raw('127.0.0.1', h1_app.port,
                     b'POST /echo HTTP/1.1\r\n'
                     b'Host: localhost\r\n'
                     b'Transfer-Encoding: chunked\r\n'
                     + headers + b'\r\n' + tail)
        assert r.status == 200
        assert r.body == expected_body


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
