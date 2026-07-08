"""Sprint 63 regression tests — Http11Probe hardening + audit bug 1.16.

Covers:

* Phase 1 (probe Cluster A) — strict RFC 9112 §7.1.1 chunk-size / chunk-ext
  grammar, chunk-data spill, and CRLF-terminator strictness.  Malformed
  chunked framing must surface as ``HTTPException(400)`` from the recipient
  (the dispatcher's typed-exception seam), never a 500 or a silent 200.
* Phase 2 (probe Cluster B) — special request-target forms: absolute-form,
  asterisk-form, CONNECT.
* Phase 3 (probe Cluster C, cheap wins) — userinfo-in-Host, non-ASCII
  request-target, ``Transfer-Encoding`` where chunked is not final,
  duplicate Content-Type.
* Bug 1.16 — ``X-Forwarded-Prefix`` is only honoured via ``TrustedProxy``,
  never straight off the wire.
"""
from http import HTTPStatus

import pytest

from blackbull.headers import Headers
from blackbull.router import HTTPException
from blackbull.server.recipient import (
    AbstractReader, HTTP1Recipient, _parse_chunk_size,
)
from blackbull.server.sender import AbstractWriter


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------

class _BufReader(AbstractReader):
    """Serves a fixed wire buffer; EOF (empty bytes) once drained."""

    def __init__(self, data: bytes) -> None:
        self._buf = bytearray(data)

    async def read(self, n: int) -> bytes:
        if not self._buf:
            return b''
        chunk = bytes(self._buf[:n])
        del self._buf[:n]
        return chunk


class _RecordingWriter(AbstractWriter):
    def __init__(self) -> None:
        self.data = bytearray()

    async def write(self, data: bytes) -> None:
        self.data += data


async def _noop_app(scope, receive, send):  # pragma: no cover - never dispatched
    pass


def _make_actor(writer=None):
    from blackbull.server.http1_actor import HTTP1Actor
    return HTTP1Actor(
        _BufReader(b''), writer or _RecordingWriter(),
        app=_noop_app, aggregator=None,
    )


def _chunked_recipient(wire: bytes) -> HTTP1Recipient:
    return HTTP1Recipient(
        _BufReader(wire),
        {'headers': [(b'transfer-encoding', b'chunked')]},
        chunk_size=64 * 1024,
    )


async def _drain_body(recipient: HTTP1Recipient) -> bytes:
    body = bytearray()
    while True:
        event = await recipient()
        if event['type'] == 'http.disconnect':
            break
        body += event.get('body', b'')
        if not event.get('more_body', False):
            break
    return bytes(body)


# ---------------------------------------------------------------------------
# Phase 1 — chunk-size token grammar (RFC 9112 §7.1.1: chunk-size = 1*HEXDIG)
# ---------------------------------------------------------------------------

class TestChunkSizeGrammar:
    @pytest.mark.parametrize('line,expected', [
        (b'5\r\n', 5),
        (b'A\r\n', 10),
        (b'a\r\n', 10),
        (b'10\r\n', 16),
        (b'5;foo=bar\r\n', 5),          # chunk-ext ignored
        (b'5 ;foo=bar\r\n', 5),         # BWS between size and ';'
        (b'5;foo\r\n', 5),              # ext-name with no value
        (b'5;a="quoted val"\r\n', 5),   # quoted-string ext-val
    ])
    def test_valid_chunk_size_lines(self, line, expected):
        assert _parse_chunk_size(line) == expected

    @pytest.mark.parametrize('line', [
        b'-1\r\n',        # SMUG-CHUNK-NEGATIVE — sign not in 1*HEXDIG
        b'+0\r\n',        # SMUG-CHUNK-SIZE-PLUS
        b'0x5\r\n',       # SMUG-CHUNK-HEX-PREFIX — int(s,16) leniency
        b'1_0\r\n',       # SMUG-CHUNK-UNDERSCORE — PEP 515 leniency
        b' 5\r\n',        # SMUG-CHUNK-LEADING-SP
        b'5 \r\n',        # SMUG-CHUNK-SIZE-TRAILING-OWS (no ext ⇒ no BWS)
        b'5;\r\n',        # SMUG-CHUNK-BARE-SEMICOLON — empty ext-name
        b'5;a@b=c\r\n',   # SMUG-CHUNK-EXT-INVALID-TOKEN — '@' not a tchar
        b'5;a=\x01\r\n',  # SMUG-CHUNK-EXT-CTRL — control char in ext-val
        b'5;a="unterminated\r\n',  # broken quoted-string ext-val
        b'\r\n',          # empty size
        b'XYZ\r\n',       # non-hex
    ])
    def test_malformed_chunk_size_lines_raise_400(self, line):
        with pytest.raises(HTTPException) as exc_info:
            _parse_chunk_size(line)
        assert exc_info.value.status == HTTPStatus.BAD_REQUEST


# ---------------------------------------------------------------------------
# Phase 1 — recipient-level framing strictness (spill / terminators)
# ---------------------------------------------------------------------------

class TestChunkedFramingStrict:
    @pytest.mark.asyncio
    async def test_valid_chunked_body_still_round_trips(self):
        wire = b'5\r\nhello\r\n6\r\n world\r\n0\r\n\r\n'
        assert await _drain_body(_chunked_recipient(wire)) == b'hello world'

    @pytest.mark.asyncio
    async def test_chunk_data_spill_raises_400(self):
        # SMUG-CHUNK-SPILL — data exceeds the declared chunk-size.
        wire = b'5\r\nhelloEXTRA\r\n0\r\n\r\n'
        recipient = _chunked_recipient(wire)
        with pytest.raises(HTTPException) as exc_info:
            await _drain_body(recipient)
        assert exc_info.value.status == HTTPStatus.BAD_REQUEST

    @pytest.mark.asyncio
    async def test_bare_cr_chunk_terminator_raises_400(self):
        # SMUG-CHUNK-BARE-CR-TERM — chunk-data terminated by CR alone.
        wire = b'5\r\nhello\r0\r\n\r\n'
        with pytest.raises(HTTPException) as exc_info:
            await _drain_body(_chunked_recipient(wire))
        assert exc_info.value.status == HTTPStatus.BAD_REQUEST

    @pytest.mark.asyncio
    async def test_bare_lf_chunk_terminator_raises_400(self):
        # SMUG-CHUNK-LF-TERM — chunk-data terminated by LF alone.
        wire = b'5\r\nhello\n0\r\n\r\n'
        with pytest.raises(HTTPException) as exc_info:
            await _drain_body(_chunked_recipient(wire))
        assert exc_info.value.status == HTTPStatus.BAD_REQUEST

    @pytest.mark.asyncio
    async def test_bare_lf_size_line_raises_400(self):
        # A chunk-size line terminated by a bare LF must not be accepted
        # (and, pre-fix, could hang the parser waiting for CRLF).
        wire = b'5\nhello\r\n0\r\n\r\n'
        with pytest.raises(HTTPException) as exc_info:
            await _drain_body(_chunked_recipient(wire))
        assert exc_info.value.status == HTTPStatus.BAD_REQUEST

    @pytest.mark.asyncio
    async def test_missing_crlf_between_chunks_raises_400(self):
        # SMUG-CHUNK-MISSING-TRAILING-CRLF — next chunk glued to the data.
        wire = b'5\r\nhello0\r\n\r\n'
        with pytest.raises(HTTPException) as exc_info:
            await _drain_body(_chunked_recipient(wire))
        assert exc_info.value.status == HTTPStatus.BAD_REQUEST

    @pytest.mark.asyncio
    async def test_framing_error_marks_recipient_broken(self):
        # The actor must close the connection after a framing 400 — the
        # byte stream is desynced, so keep-alive would be a smuggling
        # vector.  The recipient signals this via ``framing_broken``.
        recipient = _chunked_recipient(b'5\r\nhelloEXTRA\r\n0\r\n\r\n')
        with pytest.raises(HTTPException):
            await _drain_body(recipient)
        assert recipient.framing_broken is True
        assert recipient.needs_drain() is False


# ---------------------------------------------------------------------------
# Phase 2 — special request-target forms (RFC 9112 §3.2)
# ---------------------------------------------------------------------------

class TestSpecialRequestForms:
    def test_absolute_form_rewritten_to_origin_form(self):
        # COMP-ABSOLUTE-FORM — GET http://host/path must route as /path.
        actor = _make_actor()
        scope = actor._parse(b'GET http://example.com/echo?x=1 HTTP/1.1\r\n'
                             b'Host: example.com\r\n\r\n')
        assert scope['path'] == '/echo'
        assert scope['query_string'] == b'x=1'

    def test_absolute_form_authority_overrides_host(self):
        # RFC 9112 §3.2.2 — the origin server MUST ignore the Host header
        # and use the request-target's authority instead
        # (SMUG-ABSOLUTE-URI-HOST-MISMATCH).
        actor = _make_actor()
        scope = actor._parse(b'GET http://real.example/ HTTP/1.1\r\n'
                             b'Host: spoofed.example\r\n\r\n')
        assert scope['headers'].get(b'host') == b'real.example'

    def test_absolute_form_bare_authority_gets_root_path(self):
        actor = _make_actor()
        scope = actor._parse(b'GET http://example.com HTTP/1.1\r\n'
                             b'Host: example.com\r\n\r\n')
        assert scope['path'] == '/'

    def test_asterisk_form_options_flagged_for_server_level_answer(self):
        actor = _make_actor()
        scope = actor._parse(b'OPTIONS * HTTP/1.1\r\n'
                             b'Host: example.com\r\n\r\n')
        assert scope.get('_asterisk_form') is True

    def test_asterisk_form_non_options_rejected(self):
        # COMP-ASTERISK-WITH-GET — '*' is only valid for OPTIONS (§3.2.4).
        from blackbull.server.http1_actor import BadRequestError
        actor = _make_actor()
        with pytest.raises(BadRequestError):
            actor._parse(b'GET * HTTP/1.1\r\nHost: example.com\r\n\r\n')

    def test_connect_method_not_implemented(self):
        # COMP-METHOD-CONNECT — tunneling is not implemented → 501 path.
        from blackbull.server.http1_actor import NotImplementedFramingError
        actor = _make_actor()
        with pytest.raises(NotImplementedFramingError):
            actor._parse(b'CONNECT example.com:80 HTTP/1.1\r\n'
                         b'Host: example.com\r\n\r\n')


# ---------------------------------------------------------------------------
# Phase 3 — protocol validation hardening (cheap Cluster C wins)
# ---------------------------------------------------------------------------

class TestProtocolValidation:
    def test_userinfo_in_host_rejected(self):
        # COMP-HOST-WITH-USERINFO — RFC 3986 §3.2 deprecates userinfo in
        # authority; its presence in Host is an SSRF/smuggling vector.
        from blackbull.server.http1_actor import BadRequestError
        actor = _make_actor()
        with pytest.raises(BadRequestError):
            actor._parse(b'GET / HTTP/1.1\r\nHost: user@example.com\r\n\r\n')

    def test_non_ascii_request_target_rejected(self):
        # MAL-NON-ASCII-URL — raw non-ASCII bytes in the request-target.
        from blackbull.server.http1_actor import BadRequestError
        actor = _make_actor()
        with pytest.raises(BadRequestError):
            actor._parse(b'GET /\xc3\xa9 HTTP/1.1\r\nHost: example.com\r\n\r\n')

    def test_te_chunked_not_final_rejected_400(self):
        # SMUG-TE-NOT-FINAL-CHUNKED — RFC 9112 §6.1: chunked present but not
        # final ⇒ body length undeterminable ⇒ MUST 400 (not 501).
        from blackbull.server.http1_actor import BadRequestError
        actor = _make_actor()
        with pytest.raises(BadRequestError):
            actor._parse(b'POST /echo HTTP/1.1\r\nHost: x\r\n'
                         b'Transfer-Encoding: chunked, gzip\r\n\r\n')

    def test_te_unknown_coding_still_501(self):
        # gzip (chunked final absent) stays 501 Not Implemented.
        from blackbull.server.http1_actor import NotImplementedFramingError
        actor = _make_actor()
        with pytest.raises(NotImplementedFramingError):
            actor._parse(b'POST /echo HTTP/1.1\r\nHost: x\r\n'
                         b'Transfer-Encoding: gzip\r\n\r\n')

    def test_te_duplicate_chunked_rejected_400(self):
        from blackbull.server.http1_actor import BadRequestError
        actor = _make_actor()
        with pytest.raises(BadRequestError):
            actor._parse(b'POST /echo HTTP/1.1\r\nHost: x\r\n'
                         b'Transfer-Encoding: chunked, chunked\r\n\r\n')

    def test_plain_chunked_still_accepted(self):
        actor = _make_actor()
        scope = actor._parse(b'POST /echo HTTP/1.1\r\nHost: x\r\n'
                             b'Transfer-Encoding: chunked\r\n\r\n')
        assert scope['type'] == 'http'

    def test_duplicate_content_type_rejected(self):
        # COMP-DUPLICATE-CT — Content-Type is a singleton field.
        from blackbull.server.http1_actor import BadRequestError
        actor = _make_actor()
        with pytest.raises(BadRequestError):
            actor._parse(b'GET / HTTP/1.1\r\nHost: x\r\n'
                         b'Content-Type: text/plain\r\n'
                         b'Content-Type: text/html\r\n\r\n')


# ---------------------------------------------------------------------------
# Bug 1.16 — X-Forwarded-Prefix only honoured via TrustedProxy
# ---------------------------------------------------------------------------

class TestXForwardedPrefixTrust:
    def test_h1_parse_ignores_forwarded_prefix(self):
        actor = _make_actor()
        scope = actor._parse(b'GET / HTTP/1.1\r\nHost: x\r\n'
                             b'X-Forwarded-Prefix: /evil\r\n\r\n')
        assert scope['root_path'] == ''

    def test_h2_parse_headers_ignores_forwarded_prefix(self):
        # Frame construction mirrors test_parser.py's H2 dispatch harness.
        from hpack import Encoder
        from blackbull.protocol.frame import FrameFactory
        from blackbull.protocol.frame_types import FrameTypes, HeaderFrameFlags
        from blackbull.server.parser import parse_headers

        block = Encoder().encode([
            (b':method', b'GET'), (b':path', b'/'), (b':scheme', b'https'),
            (b'x-forwarded-prefix', b'/evil'),
        ])
        flags = HeaderFrameFlags.END_HEADERS | HeaderFrameFlags.END_STREAM
        raw = (len(block).to_bytes(3, 'big') + FrameTypes.HEADERS
               + bytes([flags]) + (1).to_bytes(4, 'big') + block)
        scope = parse_headers(FrameFactory().load(raw))
        assert scope.get('root_path', '') == ''

    @pytest.mark.asyncio
    async def test_trusted_proxy_sets_root_path(self):
        from blackbull.middleware.proxy import TrustedProxy
        mw = TrustedProxy(['127.0.0.1'])
        scope = {'type': 'http', 'client': ['127.0.0.1', 1234],
                 'headers': Headers([(b'x-forwarded-prefix', b'/app')])}
        called = {}

        async def call_next(scope, receive, send):
            called['root_path'] = scope.get('root_path')

        await mw(scope, None, None, call_next)
        assert called['root_path'] == '/app'

    @pytest.mark.asyncio
    async def test_untrusted_peer_prefix_ignored(self):
        from blackbull.middleware.proxy import TrustedProxy
        mw = TrustedProxy(['10.0.0.1'])
        scope = {'type': 'http', 'client': ['192.0.2.9', 1234],
                 'headers': Headers([(b'x-forwarded-prefix', b'/evil')])}
        called = {}

        async def call_next(scope, receive, send):
            called['root_path'] = scope.get('root_path', '')

        await mw(scope, None, None, call_next)
        assert called['root_path'] == ''
