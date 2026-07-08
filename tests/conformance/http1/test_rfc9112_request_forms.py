"""RFC 9112 §3.2 — request-target forms + §7.1 chunk framing, on the wire.

Sprint 63 (Http11Probe hardening).  These drive a live BlackBull server with
byte-exact requests — the probe vectors that higher-level clients refuse to
send — and assert the status BlackBull actually returns.
"""
import pytest

from .conftest import send_raw


@pytest.mark.integration
class TestRequestTargetForms:
    def test_absolute_form_routes_as_origin_form(self, h1_app):
        # COMP-ABSOLUTE-FORM — GET http://host/path must reach the /echo route.
        r = send_raw('127.0.0.1', h1_app.port,
                     b'GET http://localhost/echo HTTP/1.1\r\n'
                     b'Host: localhost\r\n\r\n')
        assert r.status == 200

    def test_absolute_form_authority_overrides_spoofed_host(self, h1_app):
        # SMUG-ABSOLUTE-URI-HOST-MISMATCH — the request-target authority is
        # definitive; a mismatched Host must not change routing.
        r = send_raw('127.0.0.1', h1_app.port,
                     b'GET http://localhost/echo HTTP/1.1\r\n'
                     b'Host: evil.example\r\n\r\n')
        assert r.status == 200

    def test_options_asterisk_answered_at_server_level(self, h1_app):
        # COMP-OPTIONS-STAR — server-wide OPTIONS is answered (2xx) with Allow,
        # not routed to a 404.
        r = send_raw('127.0.0.1', h1_app.port,
                     b'OPTIONS * HTTP/1.1\r\nHost: localhost\r\n\r\n')
        assert 200 <= r.status < 300
        assert r.header(b'allow') is not None

    def test_get_asterisk_rejected(self, h1_app):
        # COMP-ASTERISK-WITH-GET — '*' target is valid only for OPTIONS.
        r = send_raw('127.0.0.1', h1_app.port,
                     b'GET * HTTP/1.1\r\nHost: localhost\r\n\r\n')
        assert r.status == 400

    def test_connect_not_implemented(self, h1_app):
        # COMP-METHOD-CONNECT — tunneling is not implemented.
        r = send_raw('127.0.0.1', h1_app.port,
                     b'CONNECT localhost:80 HTTP/1.1\r\nHost: localhost\r\n\r\n')
        assert r.status in (400, 405, 501)

    def test_non_ascii_request_target_rejected(self, h1_app):
        # MAL-NON-ASCII-URL — raw non-ASCII bytes in the target.
        r = send_raw('127.0.0.1', h1_app.port,
                     b'GET /caf\xc3\xa9 HTTP/1.1\r\nHost: localhost\r\n\r\n')
        assert r.status == 400

    def test_userinfo_in_host_rejected(self, h1_app):
        # COMP-HOST-WITH-USERINFO.
        r = send_raw('127.0.0.1', h1_app.port,
                     b'GET /echo HTTP/1.1\r\nHost: user@localhost\r\n\r\n')
        assert r.status == 400

    def test_duplicate_content_type_rejected(self, h1_app):
        # COMP-DUPLICATE-CT.
        r = send_raw('127.0.0.1', h1_app.port,
                     b'POST /echo HTTP/1.1\r\nHost: localhost\r\n'
                     b'Content-Length: 0\r\n'
                     b'Content-Type: text/plain\r\n'
                     b'Content-Type: text/html\r\n\r\n')
        assert r.status == 400


@pytest.mark.integration
class TestTransferEncodingValidation:
    def test_te_chunked_not_final_is_400(self, h1_app):
        # SMUG-TE-NOT-FINAL-CHUNKED — chunked is present but not the final
        # coding ⇒ unframeable ⇒ 400 (not 501).
        r = send_raw('127.0.0.1', h1_app.port,
                     b'POST /echo HTTP/1.1\r\nHost: localhost\r\n'
                     b'Transfer-Encoding: chunked, gzip\r\n\r\n'
                     b'5\r\nhello\r\n0\r\n\r\n')
        assert r.status == 400

    def test_te_unknown_coding_is_501(self, h1_app):
        r = send_raw('127.0.0.1', h1_app.port,
                     b'POST /echo HTTP/1.1\r\nHost: localhost\r\n'
                     b'Transfer-Encoding: gzip\r\n\r\n')
        assert r.status == 501


@pytest.mark.integration
class TestChunkFramingOnTheWire:
    """Malformed chunk framing must answer 400, not 500 or a silent 200."""

    @pytest.mark.parametrize('size_line', [
        b'-1', b'0x5', b'+0', b'1_0', b' 5', b'5 ', b'5;', b'5;a@b=c',
    ])
    def test_malformed_chunk_size_is_400(self, h1_app, size_line):
        r = send_raw('127.0.0.1', h1_app.port,
                     b'POST /echo HTTP/1.1\r\nHost: localhost\r\n'
                     b'Transfer-Encoding: chunked\r\n\r\n'
                     + size_line + b'\r\nhello\r\n0\r\n\r\n')
        assert r.status == 400, f'{size_line!r} should be 400, got {r.status}'

    def test_chunk_data_spill_is_400(self, h1_app):
        # SMUG-CHUNK-SPILL — data longer than the declared chunk-size.
        r = send_raw('127.0.0.1', h1_app.port,
                     b'POST /echo HTTP/1.1\r\nHost: localhost\r\n'
                     b'Transfer-Encoding: chunked\r\n\r\n'
                     b'5\r\nhelloEXTRA\r\n0\r\n\r\n')
        assert r.status == 400

    def test_valid_chunked_still_200(self, h1_app):
        r = send_raw('127.0.0.1', h1_app.port,
                     b'POST /echo HTTP/1.1\r\nHost: localhost\r\n'
                     b'Transfer-Encoding: chunked\r\n\r\n'
                     b'5\r\nhello\r\n0\r\n\r\n')
        assert r.status == 200
        assert r.body == b'hello'
