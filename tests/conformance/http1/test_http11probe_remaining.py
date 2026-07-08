"""Sprint 63 remainder — wire-level round-trips for the last Http11Probe FAILs.

Each test replays the exact probe vector against a live BlackBull server:

* ``RFC9112-7.1-MISSING-HOST``   — HTTP/1.1 without Host → 400.
* ``RFC9112-2.3-INVALID-VERSION``— ``HTTP/9.9`` → 505 (was a happy 200).
* ``COMP-NO-1XX-HTTP10``         — no ``100 Continue`` to an HTTP/1.0 client.
* ``MAL-CHUNK-EXT-64K``          — 64 KiB chunk extension → 400 (was a
  ``LimitOverrunError``-backed 500 through the real asyncio StreamReader).
* ``SMUG-CHUNK-LF-TRAILER``      — bare-LF trailer terminator → 400 (was a
  hang until the probe's client timeout).
* ``SMUG-CL-*`` / ``MAL-CL-TAB-BEFORE-VALUE`` — ambiguous Content-Length → 400.
* ``NORM-UNDERSCORE-CL`` / ``-TE`` — framing-confusable underscore names → 400.
"""
from __future__ import annotations

import pytest

from .conftest import send_raw


class TestMissingHostAndVersionWire:
    def test_http11_missing_host_400(self, h1_app):
        resp = send_raw('localhost', h1_app.port, b'GET / HTTP/1.1\r\n\r\n')
        assert resp.status == 400

    def test_http10_missing_host_200(self, h1_app):
        resp = send_raw('localhost', h1_app.port, b'GET / HTTP/1.0\r\n\r\n')
        assert resp.status == 200

    def test_invalid_version_505(self, h1_app):
        resp = send_raw('localhost', h1_app.port,
                        b'GET / HTTP/9.9\r\nHost: x\r\n\r\n')
        assert resp.status == 505
        assert resp.closed

    def test_http12_still_served(self, h1_app):
        resp = send_raw('localhost', h1_app.port,
                        b'GET / HTTP/1.2\r\nHost: x\r\n\r\n')
        assert resp.status == 200


class TestExpect100ContinueWire:
    def test_http11_expect_still_gets_100(self, h1_app):
        # Regression guard: the HTTP/1.0 suppression must not remove the
        # interim 100 for HTTP/1.1 clients.
        resp = send_raw('localhost', h1_app.port,
                        b'POST /echo HTTP/1.1\r\nHost: x\r\n'
                        b'Expect: 100-continue\r\nContent-Length: 5\r\n\r\n'
                        b'hello')
        assert b'100 Continue' in resp.raw
        assert b'200' in resp.raw
        assert resp.body.endswith(b'hello')

    def test_http10_expect_gets_no_1xx(self, h1_app):
        # COMP-NO-1XX-HTTP10 — RFC 9110 §15.2: a server MUST NOT send a
        # 1xx response to an HTTP/1.0 client.
        resp = send_raw('localhost', h1_app.port,
                        b'POST /echo HTTP/1.0\r\nHost: x\r\n'
                        b'Expect: 100-continue\r\nContent-Length: 5\r\n\r\n'
                        b'hello')
        assert b'100 Continue' not in resp.raw
        assert resp.status == 200


class TestChunkFramingWire:
    def test_64k_chunk_extension_400_not_500(self, h1_app):
        # MAL-CHUNK-EXT-64K — must traverse the real asyncio.StreamReader
        # (LimitOverrunError path), which the unit fakes cannot reach.
        req = (b'POST /echo HTTP/1.1\r\nHost: x\r\n'
               b'Transfer-Encoding: chunked\r\n\r\n'
               b'5;ext=' + b'a' * 65536 + b'\r\nhello\r\n0\r\n\r\n')
        resp = send_raw('localhost', h1_app.port, req)
        assert resp.status == 400

    def test_bare_lf_trailer_terminator_400_not_timeout(self, h1_app):
        # SMUG-CHUNK-LF-TRAILER — must answer promptly, not stall until
        # the client gives up.
        req = (b'POST /echo HTTP/1.1\r\nHost: x\r\n'
               b'Transfer-Encoding: chunked\r\n\r\n'
               b'5\r\nhello\r\n0\r\n\n')
        resp = send_raw('localhost', h1_app.port, req, timeout=3.0)
        assert resp.status == 400

    def test_prohibited_trailer_field_400(self, h1_app):
        req = (b'POST /echo HTTP/1.1\r\nHost: x\r\n'
               b'Transfer-Encoding: chunked\r\n\r\n'
               b'5\r\nhello\r\n0\r\nAuthorization: Bearer evil\r\n\r\n')
        resp = send_raw('localhost', h1_app.port, req)
        assert resp.status == 400

    def test_benign_trailer_still_200(self, h1_app):
        req = (b'POST /echo HTTP/1.1\r\nHost: x\r\n'
               b'Transfer-Encoding: chunked\r\n\r\n'
               b'5\r\nhello\r\n0\r\nx-checksum: abc\r\n\r\n')
        resp = send_raw('localhost', h1_app.port, req)
        assert resp.status == 200
        assert resp.body == b'hello'


class TestContentLengthStrictWire:
    @pytest.mark.parametrize('cl_line', [
        b'Content-Length: 005',    # SMUG-CL-LEADING-ZEROS
        b'Content-Length: 00',     # SMUG-CL-DOUBLE-ZERO
        b'Content-Length: 010',    # SMUG-CL-LEADING-ZEROS-OCTAL
        b'Content-Length: 5 ',     # SMUG-CL-TRAILING-SPACE
        b'Content-Length:  5',     # SMUG-CL-EXTRA-LEADING-SP
        b'Content-Length:\t5',     # MAL-CL-TAB-BEFORE-VALUE
    ])
    def test_ambiguous_content_length_400(self, h1_app, cl_line):
        req = (b'POST /echo HTTP/1.1\r\nHost: x\r\n'
               + cl_line + b'\r\n\r\nhello')
        resp = send_raw('localhost', h1_app.port, req)
        assert resp.status == 400

    def test_canonical_content_length_still_200(self, h1_app):
        resp = send_raw('localhost', h1_app.port,
                        b'POST /echo HTTP/1.1\r\nHost: x\r\n'
                        b'Content-Length: 5\r\n\r\nhello')
        assert resp.status == 200
        assert resp.body == b'hello'


class TestUnderscoreFramingNamesWire:
    def test_underscore_content_length_400(self, h1_app):
        # NORM-UNDERSCORE-CL
        resp = send_raw('localhost', h1_app.port,
                        b'POST /echo HTTP/1.1\r\nHost: x\r\n'
                        b'Content-Length: 5\r\nContent_Length: 99\r\n\r\nhello')
        assert resp.status == 400

    def test_underscore_transfer_encoding_400(self, h1_app):
        # NORM-UNDERSCORE-TE
        resp = send_raw('localhost', h1_app.port,
                        b'POST /echo HTTP/1.1\r\nHost: x\r\n'
                        b'Content-Length: 5\r\nTransfer_Encoding: chunked\r\n\r\nhello')
        assert resp.status == 400
