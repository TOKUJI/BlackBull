"""RFC 9113 §8.3.1 — ``:authority`` validation and ASGI ``host`` mapping.

Sprint 72 F.1 (audit follow-up 2026-07-14): HTTP/2 requests must carry a
host authority — either the ``:authority`` pseudo-header or a literal
``Host`` field — and ``:authority`` must be surfaced to the application
as the ``host`` header in ``scope.headers`` (ASGI HTTP spec), mirroring
HTTP/1.1's absolute-form-overrides-Host semantics (RFC 9112 §3.2.2) and
its userinfo/grammar rejection (the 1.25 fix, Sprint 63).

Raw-frame tests drive ``HTTP2Actor`` directly via in-process fakes (same
helpers as ``test_rfc9113_gaps.py``); scope-mapping tests call
``parse_headers`` directly on ``FrameFactory``-loaded frames.
"""
from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock
from hpack import Encoder

from blackbull.headers import Headers
from blackbull.server.http2_actor import HTTP2Actor
from blackbull.server.parser import parse_headers
from blackbull.server.sender import AsyncioWriter
from blackbull.protocol.frame import FrameFactory
from blackbull.protocol.frame_types import (
    FrameTypes, ErrorCodes, HeaderFrameFlags, PseudoHeaders,
)


# ---------------------------------------------------------------------------
# Wire-format helpers (same as test_rfc9113_gaps.py)
# ---------------------------------------------------------------------------

def _make_h2_frame(type_byte: FrameTypes, flags: int = 0,
                   stream_id: int = 0, payload: bytes = b'') -> bytes:
    length = len(payload)
    return (length.to_bytes(3, 'big') + type_byte
            + bytes([flags]) + stream_id.to_bytes(4, 'big') + payload)


def _make_headers_frame(stream_id: int = 1, end_stream: bool = True,
                        fields: list[tuple[bytes, bytes]] | None = None) -> bytes:
    encoder = Encoder()
    if fields is None:
        fields = [(b':method', b'GET'), (b':path', b'/'), (b':scheme', b'https'),
                  (b':authority', b'example.com')]
    block = encoder.encode(fields)
    flags = HeaderFrameFlags.END_HEADERS
    if end_stream:
        flags |= HeaderFrameFlags.END_STREAM
    return _make_h2_frame(FrameTypes.HEADERS, flags, stream_id, block)


def _make_h2_actor(app=None):
    if app is None:
        app = AsyncMock()
    writer = MagicMock()
    writer.drain = AsyncMock()
    writer.close = MagicMock()
    handler = HTTP2Actor(None, AsyncioWriter(writer), app, aggregator=None)
    handler.send_frame = AsyncMock()
    return handler, app


def _settings() -> bytes:
    return _make_h2_frame(FrameTypes.SETTINGS, 0, 0, b'')


async def _run_with_headers(fields):
    handler, app = _make_h2_actor()
    h = _make_headers_frame(1, fields=fields)
    handler.receive = AsyncMock(side_effect=[_settings(), h, None])
    await handler.run()
    return handler, app


def _rst_streams(handler) -> list[int]:
    out = []
    for call in handler.send_frame.call_args_list:
        frame = call.args[0]
        if (hasattr(frame, 'FrameType')
                and frame.FrameType() == FrameTypes.RST_STREAM):
            out.append(frame.stream_id)
    return out


def _assert_malformed(handler):
    """Malformed request HEADERS ⇒ stream error RST_STREAM PROTOCOL_ERROR."""
    for call in handler.send_frame.call_args_list:
        frame = call.args[0]
        if (hasattr(frame, 'FrameType')
                and frame.FrameType() == FrameTypes.RST_STREAM
                and frame.stream_id == 1
                and frame.error_code == ErrorCodes.PROTOCOL_ERROR):
            return
    pytest.fail(
        'Expected RST_STREAM(PROTOCOL_ERROR) on stream 1.  Frames sent: '
        f'{[c.args[0] for c in handler.send_frame.call_args_list]}')


def _load_frame(fields: list[tuple[bytes, bytes]]):
    """Build a HEADERS frame through the real wire parser."""
    block = Encoder().encode(fields)
    flags = HeaderFrameFlags.END_HEADERS | HeaderFrameFlags.END_STREAM
    raw = (len(block).to_bytes(3, 'big') + FrameTypes.HEADERS
           + bytes([flags]) + (1).to_bytes(4, 'big') + block)
    return FrameFactory().load(raw)


_PSEUDO_TRIO = [(b':method', b'GET'), (b':path', b'/'), (b':scheme', b'https')]


# ═══════════════════════════════════════════════════════════════════════
# F.1a — presence: neither :authority nor Host ⇒ malformed
# ═══════════════════════════════════════════════════════════════════════

class TestAuthorityPresence:
    """RFC 9113 §8.3.1: an ``http``/``https`` request without ``:authority``
    must carry a ``Host`` header field; otherwise it is malformed."""

    @pytest.mark.asyncio
    async def test_missing_authority_and_host_is_malformed(self):
        handler, app = await _run_with_headers(list(_PSEUDO_TRIO))
        _assert_malformed(handler)
        assert app.await_count == 0

    @pytest.mark.asyncio
    async def test_authority_only_is_accepted(self):
        handler, app = await _run_with_headers(
            _PSEUDO_TRIO + [(b':authority', b'example.com')])
        assert 1 not in _rst_streams(handler)
        assert app.await_count == 1

    @pytest.mark.asyncio
    async def test_host_only_is_accepted(self):
        handler, app = await _run_with_headers(
            _PSEUDO_TRIO + [(b'host', b'example.com')])
        assert 1 not in _rst_streams(handler)
        assert app.await_count == 1

    @pytest.mark.asyncio
    async def test_multiple_host_without_authority_is_malformed(self):
        """Mirror of H1's multiple-Host smuggling rejection."""
        handler, app = await _run_with_headers(
            _PSEUDO_TRIO + [(b'host', b'a.example'), (b'host', b'b.example')])
        _assert_malformed(handler)
        assert app.await_count == 0


# ═══════════════════════════════════════════════════════════════════════
# F.1a — grammar: userinfo / delimiters / whitespace ⇒ malformed
# ═══════════════════════════════════════════════════════════════════════

class TestAuthorityGrammar:
    """RFC 9113 §8.3.1: ``:authority`` MUST NOT include userinfo ('@');
    delimiters and whitespace are equally not valid in a host authority
    (RFC 3986 §3.2) — same grammar H1 enforces via ``_validate_host``."""

    @pytest.mark.asyncio
    async def test_userinfo_in_authority_is_malformed(self):
        handler, app = await _run_with_headers(
            _PSEUDO_TRIO + [(b':authority', b'user@example.com')])
        _assert_malformed(handler)
        assert app.await_count == 0

    @pytest.mark.parametrize('bad_byte', [b'/', b'?', b'#', b' ', b'\t', b'@'])
    @pytest.mark.asyncio
    async def test_forbidden_byte_in_authority_is_malformed(self, bad_byte):
        handler, app = await _run_with_headers(
            _PSEUDO_TRIO + [(b':authority', b'exam' + bad_byte + b'ple.com')])
        _assert_malformed(handler)

    @pytest.mark.asyncio
    async def test_empty_authority_is_malformed(self):
        handler, app = await _run_with_headers(
            _PSEUDO_TRIO + [(b':authority', b'')])
        _assert_malformed(handler)

    @pytest.mark.asyncio
    async def test_userinfo_in_host_without_authority_is_malformed(self):
        """The Host fallback is held to the same grammar as H1."""
        handler, app = await _run_with_headers(
            _PSEUDO_TRIO + [(b'host', b'user@example.com')])
        _assert_malformed(handler)


# ═══════════════════════════════════════════════════════════════════════
# F.1b — ASGI mapping: :authority → host in scope.headers
# ═══════════════════════════════════════════════════════════════════════

class TestAuthorityHostMapping:
    """ASGI HTTP spec: a request's ``:authority`` pseudo-header is surfaced
    as the ``host`` header; a literal ``Host`` is replaced (same semantics
    as H1's absolute-form override, RFC 9112 §3.2.2)."""

    def test_authority_maps_to_host_in_scope(self):
        frame = _load_frame(
            _PSEUDO_TRIO + [(b':authority', b'example.com:8443')])
        scope = parse_headers(frame)
        assert not frame.malformed
        assert scope.headers.get(b'host') == b'example.com:8443'
        assert scope.headers.get(b':authority') == b''  # never leaks

    def test_authority_replaces_literal_host(self):
        frame = _load_frame(
            _PSEUDO_TRIO + [(b':authority', b'real.example'),
                            (b'host', b'spoofed.example')])
        scope = parse_headers(frame)
        assert not frame.malformed
        assert scope.headers.getlist(b'host') == [
            (b'host', b'real.example')]

    def test_host_fallback_kept_when_no_authority(self):
        frame = _load_frame(_PSEUDO_TRIO + [(b'host', b'example.com')])
        scope = parse_headers(frame)
        assert not frame.malformed
        assert scope.headers.get(b'host') == b'example.com'

    def test_missing_both_marks_frame_malformed(self):
        frame = _load_frame(list(_PSEUDO_TRIO))
        parse_headers(frame)
        assert frame.malformed

    def test_ws_over_h2_scope_gets_host(self):
        """RFC 8441 Extended CONNECT scopes get the same host mapping."""
        frame = _load_frame([
            (b':method', b'CONNECT'), (b':protocol', b'websocket'),
            (b':scheme', b'https'), (b':path', b'/ws'),
            (b':authority', b'example.com:8443'),
        ])
        scope = parse_headers(frame)
        assert not frame.malformed
        assert scope.type == 'websocket'
        assert scope.headers.get(b'host') == b'example.com:8443'

    def test_plain_connect_headers_untouched(self):
        """RFC 9113 §8.5 gives :authority different semantics on plain
        CONNECT — no mapping, no presence rejection (out of F.1 scope)."""
        frame = _load_frame([(b':method', b'CONNECT'),
                             (b':authority', b'example.com:443')])
        scope = parse_headers(frame)
        assert not frame.malformed
        assert scope.headers.get(b'host') == b''


# ═══════════════════════════════════════════════════════════════════════
# F.1c — server push reads the mapped host
# ═══════════════════════════════════════════════════════════════════════

class TestPushPromiseAuthority:
    """PUSH_PROMISE synthesises ``:authority`` from the parent scope's
    (now guaranteed) ``host`` header instead of falling back to
    ``localhost``."""

    @pytest.mark.asyncio
    async def test_push_promise_uses_scope_host(self):
        handler, app = _make_h2_actor()
        parent = handler.root_stream.add_child(1)
        parent.scope = {
            'type': 'http', 'scheme': 'https',
            'headers': Headers([(b'host', b'example.com:8443')]),
        }
        await handler._handle_push({'path': '/style.css'}, 1)
        pp = None
        for call in handler.send_frame.call_args_list:
            frame = call.args[0]
            if (hasattr(frame, 'FrameType')
                    and frame.FrameType() == FrameTypes.PUSH_PROMISE):
                pp = frame
                break
        assert pp is not None, 'no PUSH_PROMISE sent'
        assert pp.pseudo_headers[PseudoHeaders.AUTHORITY] == 'example.com:8443'
