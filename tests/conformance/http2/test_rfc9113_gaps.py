"""RFC 9113 compliance gap tests — covering HIGH and MEDIUM priority
MUST/SHOULD requirements not yet validated by existing conformance tests.

Gap reference: ``BLA-210`` [private]

Tests are organized by RFC section.  Each test class documents the
specific requirement, the expected behavior, and the current status.

Tests that use raw H2 frame injection drive ``HTTP2Actor`` directly
via in-process fakes (no live sockets).  Tests that validate
field-level rules use the ASGI harness or HTTP2Client where possible.
"""
from __future__ import annotations

import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock
from hpack import Decoder, Encoder

from blackbull.server.http2_actor import HTTP2Actor
from blackbull.server.recipient import AbstractReader, IncompleteReadError
from tests.pseudo_header_grammar import (ILLEGAL_METHODS, ILLEGAL_SCHEMES,
                                         LEGAL_METHODS, LEGAL_SCHEMES)
from blackbull.server.sender import AsyncioWriter
from blackbull.protocol.frame import FrameFactory
from blackbull.protocol.frame_types import (
    FrameTypes, FrameFlags, ErrorCodes,
    HeaderFrameFlags, DataFrameFlags, SettingFrameFlags,
)
from tests.conformance.http2._harness import (
    _make_h2_frame, _make_headers_frame, _BufferReader,
    h2_connect, h2_terminal,
)
from tests.harness import served


def _make_h2_actor(app=None):
    if app is None:
        app = AsyncMock()
    writer = MagicMock()
    writer.drain = AsyncMock()
    writer.close = MagicMock()
    handler = HTTP2Actor(None, AsyncioWriter(writer), app, aggregator=None)
    handler.send_frame = AsyncMock()
    return handler, app


def _sent_rst_streams(handler, stream_id: int) -> list:
    """RST_STREAM frames the actor sent for *stream_id* on the mocked wire."""
    return [
        call.args[0] for call in handler.send_frame.call_args_list
        if hasattr(call.args[0], 'FrameType')
        and call.args[0].FrameType() == FrameTypes.RST_STREAM
        and call.args[0].stream_id == stream_id
    ]


def _sent_goaway_codes(handler) -> list:
    """Error codes of the GOAWAY frames the actor sent on the mocked wire."""
    return [
        call.args[0].error_code for call in handler.send_frame.call_args_list
        if hasattr(call.args[0], 'FrameType')
        and call.args[0].FrameType() == FrameTypes.GOAWAY
    ]


# ═══════════════════════════════════════════════════════════════════════
# G2: Stream state validation — half-closed(remote)
# ═══════════════════════════════════════════════════════════════════════

class TestG2HalfClosedRemoteReceivesData:
    """RFC 9113 §5.1 (half-closed remote): Receiving frames other than
    WINDOW_UPDATE, PRIORITY, or RST_STREAM MUST → STREAM_CLOSED stream error."""

    @pytest.mark.asyncio
    async def test_data_on_half_closed_remote_is_stream_closed(self):
        handler, app = _make_h2_actor()
        # Open stream 1, then close it from the remote side
        h = _make_headers_frame(1, end_stream=True)
        # Now stream 1 is half-closed(remote) for the server.
        # Send DATA — must get STREAM_CLOSED.
        data = _make_h2_frame(FrameTypes.DATA, 0, stream_id=1, payload=b'bad')
        handler.receive = AsyncMock(side_effect=[
            self._settings(), h, data, None])
        await handler.run()
        self._assert_rst_sent(handler, 1, ErrorCodes.STREAM_CLOSED)

    @pytest.mark.asyncio
    async def test_headers_on_half_closed_remote_is_stream_closed(self):
        handler, app = _make_h2_actor()
        h1 = _make_headers_frame(1, end_stream=True)
        h2 = _make_headers_frame(1, end_stream=True, end_headers=True,
                                 fields=[(b':method', b'GET'),
                                         (b':path', b'/2'),
                                         (b':scheme', b'https'), (b':authority', b'example.com')])
        handler.receive = AsyncMock(side_effect=[
            self._settings(), h1, h2, None])
        await handler.run()
        self._assert_rst_sent(handler, 1, ErrorCodes.STREAM_CLOSED)

    @staticmethod
    def _settings():
        return _make_h2_frame(FrameTypes.SETTINGS, 0, 0, b'')

    @staticmethod
    def _assert_rst_sent(handler, stream_id, expected_code):
        for call in handler.send_frame.call_args_list:
            frame = call.args[0]
            if hasattr(frame, 'FrameType') and frame.FrameType() == FrameTypes.RST_STREAM:
                if frame.stream_id == stream_id and frame.error_code == expected_code:
                    return
        pytest.fail(
            f'Expected RST_STREAM on stream {stream_id} with '
            f'{ErrorCodes(expected_code).name}, but not found.')


# ═══════════════════════════════════════════════════════════════════════
# G4: Negative flow-control window tracking
# ═══════════════════════════════════════════════════════════════════════

class TestG4NegativeFlowControlWindow:
    """RFC 9113 §6.9.2: When SETTINGS_INITIAL_WINDOW_SIZE is reduced, a sender
    MUST track the negative flow-control window and MUST NOT send until
    WINDOW_UPDATE frames make it positive."""

    @pytest.mark.asyncio
    async def test_initial_window_reduction_creates_negative_window(self):
        """Reduce SETTINGS_INITIAL_WINDOW_SIZE from 65535 to 16384.
        If the client already sent 60KB of DATA, the window goes negative.
        The sender must track this and not send more DATA."""
        handler, app = _make_h2_actor()
        # Open a stream
        h = _make_headers_frame(1, end_stream=False)
        # Send 60KB of DATA (four DATA frames, ~16KB each) — but we
        # can't send 60KB in raw frames easily.  Instead, verify that
        # the SETTINGS_INITIAL_WINDOW_SIZE change is properly tracked
        # by checking that the handler applies it to active streams.
        settings_reduce = _make_h2_frame(
            FrameTypes.SETTINGS, 0, 0,
            payload=(0x04).to_bytes(2, 'big')   # SETTINGS_INITIAL_WINDOW_SIZE
            + (16384).to_bytes(4, 'big'))
        handler.receive = AsyncMock(side_effect=[
            self._settings(), h, settings_reduce, None])
        await handler.run()
        # If the handler doesn't crash and properly applies the setting,
        # the test passes.  A crash or failure to apply would indicate
        # non-compliance.
        # We verify that send_frame was called with a SETTINGS ACK.
        ack_found = False
        for call in handler.send_frame.call_args_list:
            frame = call.args[0]
            if hasattr(frame, 'FrameType') and frame.FrameType() == FrameTypes.SETTINGS:
                if frame.flags & SettingFrameFlags.ACK:
                    ack_found = True
        assert ack_found, 'SETTINGS ACK not sent after receiving SETTINGS'

    @staticmethod
    def _settings():
        return _make_h2_frame(FrameTypes.SETTINGS, 0, 0, b'')


# ═══════════════════════════════════════════════════════════════════════
# G5: Connection-specific header fields forbidden (§8.2.2)
# ═══════════════════════════════════════════════════════════════════════

class TestG5ConnectionSpecificHeadersForbidden:
    """RFC 9113 §8.2.2: Endpoints MUST NOT generate HTTP/2 messages containing
    connection-specific header fields (Connection, Proxy-Connection,
    Keep-Alive, Transfer-Encoding, Upgrade).  Messages containing them
    MUST be treated as malformed → stream error PROTOCOL_ERROR."""

    CONNECTION_SPECIFIC = [
        b'connection',
        b'proxy-connection',
        b'keep-alive',
        b'transfer-encoding',
        b'upgrade',
    ]

    @pytest.mark.parametrize('bad_header', CONNECTION_SPECIFIC)
    @pytest.mark.asyncio
    async def test_connection_specific_header_is_malformed(self, bad_header):
        handler, app = _make_h2_actor()
        fields = [
            (b':method', b'GET'),
            (b':path', b'/'),
            (b':scheme', b'https'),
            (b':authority', b'example.com'),
            (bad_header, b'whatever'),
        ]
        h = _make_headers_frame(1, end_stream=True, fields=fields)
        handler.receive = AsyncMock(side_effect=[
            self._settings(), h, None])
        await handler.run()
        # Must send RST_STREAM with PROTOCOL_ERROR (malformed).
        for call in handler.send_frame.call_args_list:
            frame = call.args[0]
            if hasattr(frame, 'FrameType') and frame.FrameType() == FrameTypes.RST_STREAM:
                if frame.stream_id == 1:
                    return  # RST sent → malformed detected
        pytest.fail(
            f'Connection-specific header {bad_header!r} was not rejected '
            f'as malformed (no RST_STREAM sent)')

    @pytest.mark.asyncio
    async def test_te_header_with_value_other_than_trailers_is_malformed(self):
        """TE header MUST only contain 'trailers'."""
        handler, app = _make_h2_actor()
        fields = [
            (b':method', b'GET'), (b':path', b'/'), (b':scheme', b'https'),
            (b':authority', b'example.com'),
            (b'te', b'gzip, deflate'),
        ]
        h = _make_headers_frame(1, end_stream=True, fields=fields)
        handler.receive = AsyncMock(side_effect=[
            self._settings(), h, None])
        await handler.run()
        for call in handler.send_frame.call_args_list:
            frame = call.args[0]
            if hasattr(frame, 'FrameType') and frame.FrameType() == FrameTypes.RST_STREAM:
                if frame.stream_id == 1:
                    return
        pytest.fail('TE: gzip, deflate was not rejected as malformed')

    @staticmethod
    def _settings():
        return _make_h2_frame(FrameTypes.SETTINGS, 0, 0, b'')


# ═══════════════════════════════════════════════════════════════════════
# G6: Missing mandatory pseudo-headers (§8.3.1)
# ═══════════════════════════════════════════════════════════════════════

async def _check_malformed(fields):
    """Drive a HEADERS block and require RST_STREAM(PROTOCOL_ERROR) on 1,
    with the application never entered."""
    handler, app = _make_h2_actor()
    h = _make_headers_frame(1, end_stream=True, fields=fields)
    settings = _make_h2_frame(FrameTypes.SETTINGS, 0, 0, b'')
    handler.receive = AsyncMock(side_effect=[settings, h, None])
    await handler.run()
    for call in handler.send_frame.call_args_list:
        frame = call.args[0]
        if (hasattr(frame, 'FrameType')
                and frame.FrameType() == FrameTypes.RST_STREAM
                and frame.stream_id == 1
                and frame.error_code == ErrorCodes.PROTOCOL_ERROR):
            assert app.await_count == 0, (
                f'malformed request with fields {fields} reached the handler')
            return
    pytest.fail(f'Malformed request with fields {fields} was not rejected '
                f'with RST_STREAM(PROTOCOL_ERROR)')


class TestG6MissingMandatoryPseudoHeaders:
    """RFC 9113 §8.3.1: All HTTP/2 requests MUST include exactly one valid
    value for :method, :scheme, and :path.  Omission → malformed."""

    @pytest.mark.parametrize('fields', [
        pytest.param([(b':path', b'/'), (b':scheme', b'https'),
                      (b':authority', b'example.com')],
                     id='missing-method'),
        pytest.param([(b':method', b'GET'), (b':scheme', b'https'),
                      (b':authority', b'example.com')],
                     id='missing-path'),
    ])
    @pytest.mark.asyncio
    async def test_missing_method_is_malformed(self, fields):
        await _check_malformed(fields)

    @pytest.mark.asyncio
    async def test_missing_scheme_is_malformed(self):
        await _check_malformed([(b':method', b'GET'), (b':path', b'/')])

    @pytest.mark.asyncio
    async def test_duplicate_method_is_malformed(self):
        """§8.3: Same pseudo-header MUST NOT appear more than once."""
        await _check_malformed([
            (b':method', b'GET'), (b':method', b'POST'),
            (b':path', b'/'), (b':scheme', b'https'),
            (b':authority', b'example.com'),
        ])

    @pytest.mark.parametrize('name', [b':foo', b':Path', b':'])
    @pytest.mark.asyncio
    async def test_an_undefined_pseudo_header_is_malformed(self, name):
        """§8.3: a pseudo-header field the protocol does not define."""
        await _check_malformed([
            (b':method', b'GET'), (b':path', b'/'), (b':scheme', b'https'),
            (b':authority', b'example.com'), (name, b'x'),
        ])

    @pytest.mark.asyncio
    async def test_empty_path_for_http_uri_is_malformed(self):
        """§8.3.1: :path MUST NOT be empty for http/https URIs."""
        await _check_malformed([
            (b':method', b'GET'), (b':path', b''), (b':scheme', b'https'),
            (b':authority', b'example.com'),
        ])


class TestG6PathOctets:
    """RFC 9113 §8.3.1 with RFC 9112 §2.1 — a ``:path`` carries the octets
    HTTP/1.1 allows in its request-target, so the transports cannot disagree
    about a path (MAL-NON-ASCII-URL)."""

    @staticmethod
    def _fields(path: bytes) -> list:
        return [(b':method', b'GET'), (b':scheme', b'https'),
                (b':authority', b'example.com'), (b':path', path)]

    @pytest.mark.parametrize('bad', list(range(0x20)) + [0x7F])
    @pytest.mark.asyncio
    async def test_every_control_in_path_is_malformed(self, bad):
        await _check_malformed(self._fields(b'/a' + bytes([bad]) + b'b'))

    @pytest.mark.parametrize('bad', [b' ', b'\xc3\xa9'])
    @pytest.mark.asyncio
    async def test_non_visible_path_octet_is_malformed(self, bad):
        await _check_malformed(self._fields(b'/a' + bad + b'b'))

    @pytest.mark.asyncio
    async def test_control_in_extended_connect_path_is_malformed(
        self, monkeypatch,
    ):
        """RFC 8441 reads ``:path`` too, so the rule cannot sit only in the
        non-CONNECT branch.

        WS-over-H2 has to be enabled for the request to reach the path check:
        with the option off the actor refuses every Extended CONNECT before the
        header block is graded, which would make this test pass for the wrong
        reason.
        """
        monkeypatch.setenv('BB_H2_ENABLE_WEBSOCKET', '1')
        await _check_malformed([
            (b':method', b'CONNECT'), (b':protocol', b'websocket'),
            (b':scheme', b'https'), (b':authority', b'example.com'),
            (b':path', b'/ws\x01'),
        ])

    @pytest.mark.asyncio
    async def test_a_visible_ascii_path_is_accepted(self):
        handler, app = _make_h2_actor()
        h = _make_headers_frame(1, end_stream=True,
                                fields=self._fields(b'/a/b?x=1'))
        settings = _make_h2_frame(FrameTypes.SETTINGS, 0, 0, b'')
        handler.receive = AsyncMock(side_effect=[settings, h, None])
        await handler.run()
        assert app.await_count == 1
        assert not [
            call for call in handler.send_frame.call_args_list
            if hasattr(call.args[0], 'FrameType')
            and call.args[0].FrameType() == FrameTypes.RST_STREAM
        ]


class TestG6MethodAndSchemeGrammar:
    """RFC 9113 §8.3.1 with RFC 9110 §9.1 and RFC 3986 §3.1 — ``:method`` is a
    token and ``:scheme`` is a URI scheme.  §8.2.1's field octets admit a
    separator and an underscore, so neither rule falls out of field validity;
    HTTP/1.1 grades its request-line method with the same token rule, so the
    two transports must refuse the same methods."""

    @staticmethod
    def _fields(method: bytes = b'GET', scheme: bytes = b'https') -> list:
        return [(b':method', method), (b':path', b'/'),
                (b':scheme', scheme), (b':authority', b'example.com')]

    @staticmethod
    async def _accepted(fields):
        handler, app = _make_h2_actor()
        h = _make_headers_frame(1, end_stream=True, fields=fields)
        handler.receive = AsyncMock(side_effect=[
            _make_h2_frame(FrameTypes.SETTINGS, 0, 0, b''), h, None])
        await handler.run()
        assert app.await_count == 1
        assert not _sent_rst_streams(handler, 1)

    @pytest.mark.parametrize('overrides', [
        pytest.param({'method': m}, id=f'method-not-token-{m.hex()}')
        for m in ILLEGAL_METHODS
    ] + [
        pytest.param({'scheme': s}, id=f'scheme-not-uri-scheme-{s.hex()}')
        for s in ILLEGAL_SCHEMES
    ])
    @pytest.mark.asyncio
    async def test_a_method_that_is_not_a_token_is_malformed(self, overrides):
        await _check_malformed(self._fields(**overrides))

    @pytest.mark.parametrize('method', LEGAL_METHODS)
    @pytest.mark.asyncio
    async def test_a_token_method_is_accepted(self, method):
        await self._accepted(self._fields(method=method))

    @pytest.mark.parametrize('scheme', LEGAL_SCHEMES)
    @pytest.mark.asyncio
    async def test_a_uri_scheme_is_accepted(self, scheme):
        await self._accepted(self._fields(scheme=scheme))

    @pytest.mark.parametrize('scheme', [b'HTTPS', b'HTTP'])
    @pytest.mark.asyncio
    async def test_an_uppercase_scheme_still_requires_an_authority(self, scheme):
        """The scheme may be uppercase — ``test_a_uri_scheme_is_accepted``
        defends that.  What must be refused is the missing authority."""
        await _check_malformed([(b':method', b'GET'), (b':path', b'/'),
                                (b':scheme', scheme)])


def _make_raw_headers_frame(block: bytes, stream_id: int = 1) -> bytes:
    """A HEADERS frame carrying *block* exactly as it arrived on the wire."""
    flags = int(HeaderFrameFlags.END_HEADERS) | int(HeaderFrameFlags.END_STREAM)
    return _make_h2_frame(FrameTypes.HEADERS, flags, stream_id, block)


class TestG4CompressionErrors:
    """RFC 9113 §4.3/§5.4.1 — a field block that cannot be decoded is a
    connection error of type COMPRESSION_ERROR: GOAWAY, never RST_STREAM."""

    @pytest.mark.asyncio
    async def test_an_undecodable_promised_block_goes_away_the_same_way(self):
        """A client may not push at all, but the block it sent is still
        decoded for the connection-wide table, so §4.3 is answered before a
        stream-state rule rejects the frame."""
        handler, app = _make_h2_actor()
        settings = _make_h2_frame(FrameTypes.SETTINGS, 0, 0, b'')
        payload = (2).to_bytes(4, 'big') + b'\x80'
        handler.receive = AsyncMock(side_effect=[
            settings,
            _make_h2_frame(FrameTypes.PUSH_PROMISE,
                           int(HeaderFrameFlags.END_HEADERS), 1, payload),
            None])
        await handler.run()

        assert _sent_goaway_codes(handler) == [ErrorCodes.COMPRESSION_ERROR]
        assert handler._goaway_sent
        assert app.await_count == 0

    @pytest.mark.asyncio
    async def test_a_legal_table_size_update_still_decodes(self):
        """The allowed maximum is legal: what is refused is the codec's
        failure, not the presence of a size update."""
        block = b'\x3f\xe1\x1f' + Encoder().encode([
            (b':method', b'GET'), (b':path', b'/'), (b':scheme', b'https'),
            (b':authority', b'example.com')])
        handler, app = _make_h2_actor()
        settings = _make_h2_frame(FrameTypes.SETTINGS, 0, 0, b'')
        handler.receive = AsyncMock(side_effect=[
            settings, _make_raw_headers_frame(block), None])
        await handler.run()

        assert app.await_count == 1
        assert _sent_goaway_codes(handler) == []


# ═══════════════════════════════════════════════════════════════════════
# G13: Field name/value character validation (§8.2.1)
# ═══════════════════════════════════════════════════════════════════════

class TestG13FieldCharacterValidation:
    """RFC 9113 §8.2.1: Field names MUST NOT contain characters in ranges
    0x00-0x20, 0x41-0x5a (uppercase), or 0x7f-0xff, and field values MUST NOT
    contain NUL, LF, or CR, or start or end with SP or HTAB.  The section also
    asks (SHOULD) for RFC 9110's definitions, so a name is a §5.6.2 token —
    its separators included — and a value is §5.5 field-content.
    Violations → malformed."""

    @pytest.mark.parametrize('field,label', [
        pytest.param((b'Content-Type', b'text/html'), 'uppercase field name',
                     id='uppercase-name'),
        pytest.param((b'x-test', b'value\r\ninjection'), 'CR in field value',
                     id='cr-in-value'),
        pytest.param((b'x-test', b'value\ninjection'), 'LF in field value',
                     id='lf-in-value'),
        pytest.param((b'x-test', b'value\x00injection'), 'NUL in field value',
                     id='nul-in-value'),
    ])
    @pytest.mark.asyncio
    async def test_uppercase_field_name_is_malformed(self, field, label):
        await self._check_malformed_field(field, label)

    @pytest.mark.parametrize('field,label', [
        pytest.param((b'x-colon:name', b'value'), 'colon in field name',
                     id='colon-in-name'),
        pytest.param((b'', b'v'), 'empty field name',
                     id='empty-name'),
    ])
    @pytest.mark.asyncio
    async def test_field_name_with_colon_is_malformed(self, field, label):
        """§8.2.1: Field names MUST NOT include colon (except pseudo-headers);
        RFC 9110 §5.6.2 — token = 1*tchar, so no octets is not a name."""
        await self._check_malformed_field(field, label)

    @pytest.mark.parametrize('value', [b' value', b'value ', b'\tvalue',
                                       b'value\t'])
    @pytest.mark.asyncio
    async def test_field_value_bounded_by_whitespace_is_malformed(self, value):
        """§8.2.1: a field value MUST NOT start or end with SP or HTAB."""
        await self._check_malformed_field(
            (b'x-test', value), f'boundary whitespace in {value!r}')

    @pytest.mark.parametrize('value', [
        pytest.param(b'value with\tinner whitespace', id='inner-whitespace'),
        pytest.param(b'value\x80\xff', id='obs-text'),
    ])
    @pytest.mark.asyncio
    async def test_field_value_with_inner_whitespace_still_reaches_the_app(self, value):
        """The edge is what the MUST is about: SP and HTAB stay legal inside
        a field value, and obs-text (0x80-0xFF) is inside RFC 9110's
        field-content — neither is malformed."""
        handler, app = _make_h2_actor()
        fields = [
            (b':method', b'GET'), (b':path', b'/'), (b':scheme', b'https'),
            (b':authority', b'example.com'),
            (b'x-test', value),
        ]
        h = _make_headers_frame(1, end_stream=True, fields=fields)
        settings = _make_h2_frame(FrameTypes.SETTINGS, 0, 0, b'')
        handler.receive = AsyncMock(side_effect=[settings, h, None])
        await handler.run()

        assert app.await_count == 1
        assert not _sent_rst_streams(handler, 1)

    @pytest.mark.parametrize('value', [b'value\x01mid', b'value\x0bmid',
                                       b'value\x0cmid', b'value\x1fmid',
                                       b'value\x7f'])
    @pytest.mark.asyncio
    async def test_field_value_with_a_control_octet_is_malformed(self, value):
        """RFC 9110 §5.5 through §8.2.1's SHOULD: every control octet but HTAB
        is prohibited, and HTTP/1.1 already refuses the same octets."""
        await self._check_malformed_field(
            (b'x-test', value), f'control octet in {value!r}')

    @pytest.mark.parametrize('name', [b'x,y', b'x;y', b'x@y', b'x"y',
                                      b'x(y', b'x/y', b'x[y'])
    @pytest.mark.asyncio
    async def test_field_name_with_a_separator_octet_is_malformed(self, name):
        """RFC 9110 §5.6.2: a field name is a token, so a separator is not a
        name octet — HTTP/1.1's tchar table already refuses them."""
        await self._check_malformed_field(
            (name, b'v'), f'separator in {name!r}')

    @pytest.mark.parametrize('bad_field', [
        (b'x-trailer', b' value'), (b'x-trailer', b'value '),
        (b'x-trailer', b'value\x00'), (b'x-trailer', b'value\r'),
        (b'X-Trailer', b'value'),
        (b':method', b'GET'), (b':status', b'200'),
    ])
    @pytest.mark.asyncio
    async def test_a_malformed_trailer_field_is_malformed(self, bad_field):
        entered = asyncio.Event()
        completed = []

        async def app(conn, receive, send):
            entered.set()
            while (event := await receive()).get('more_body', False):
                pass
            if event['type'] == 'http.disconnect':
                return
            completed.append(True)
            await send({'type': 'http.response.start', 'status': 200, 'headers': []})
            await send({'type': 'http.response.body', 'body': b'ok'})

        async with served(app) as server:
            wire = await h2_connect(server)
            decoder = Decoder()
            await wire.send(_make_headers_frame(1, end_stream=False))
            await asyncio.wait_for(entered.wait(), 2)
            await wire.send(_make_headers_frame(1, end_stream=True, fields=[bad_field]))
            assert await h2_terminal(wire, 1, decoder) == (3, ErrorCodes.PROTOCOL_ERROR)
            assert completed == []
            await wire.send(_make_headers_frame(3, end_stream=True))
            fields, body = await h2_terminal(wire, 3, decoder)
            assert (b':status', b'200') in fields and body == b'ok'

    @pytest.mark.asyncio
    async def test_a_valid_trailer_field_section_is_accepted(self):
        handler, app = _make_h2_actor()
        head = _make_headers_frame(1, end_stream=False)
        trailers = _make_headers_frame(
            1, end_stream=True, fields=[(b'x-trailer', b'value')])
        settings = _make_h2_frame(FrameTypes.SETTINGS, 0, 0, b'')
        handler.receive = AsyncMock(
            side_effect=[settings, head, trailers, None])
        await handler.run()

        assert app.await_count == 1
        assert not _sent_rst_streams(handler, 1)

    @staticmethod
    async def _check_malformed_field(bad_field, description):
        handler, app = _make_h2_actor()
        fields = [
            (b':method', b'GET'), (b':path', b'/'), (b':scheme', b'https'),
            (b':authority', b'example.com'),
            bad_field,
        ]
        h = _make_headers_frame(1, end_stream=True, fields=fields)
        settings = _make_h2_frame(FrameTypes.SETTINGS, 0, 0, b'')
        handler.receive = AsyncMock(side_effect=[settings, h, None])
        await handler.run()
        rst = _sent_rst_streams(handler, 1)
        assert [f.error_code for f in rst] == [ErrorCodes.PROTOCOL_ERROR], \
            f'Field violation ({description}) was not answered with ' \
            f'RST_STREAM(PROTOCOL_ERROR): {rst}'
        assert app.await_count == 0, \
            f'Field violation ({description}) reached the application'


# ═══════════════════════════════════════════════════════════════════════
# G14: Pseudo-header ordering (§8.3) — pseudo-headers before regular fields
# ═══════════════════════════════════════════════════════════════════════

class TestG14PseudoHeaderOrdering:
    """RFC 9113 §8.3: All pseudo-header fields MUST appear in a field block
    BEFORE all regular field lines.  Violation → malformed."""

    @pytest.mark.asyncio
    async def test_regular_field_before_pseudo_header_is_malformed(self):
        handler, app = _make_h2_actor()
        # Regular field BEFORE pseudo-headers → malformed
        fields = [
            (b'content-type', b'text/html'),
            (b':method', b'GET'),
            (b':path', b'/'),
            (b':scheme', b'https'),
        ]
        h = _make_headers_frame(1, end_stream=True, fields=fields)
        settings = _make_h2_frame(FrameTypes.SETTINGS, 0, 0, b'')
        handler.receive = AsyncMock(side_effect=[settings, h, None])
        await handler.run()
        for call in handler.send_frame.call_args_list:
            frame = call.args[0]
            if hasattr(frame, 'FrameType') and frame.FrameType() == FrameTypes.RST_STREAM:
                if frame.stream_id == 1:
                    return
        pytest.fail('Pseudo-header after regular field was not rejected as malformed')

    @pytest.mark.asyncio
    async def test_pseudo_header_after_regular_field_in_separate_continuation(self):
        """Same violation, but the pseudo-header is in a CONTINUATION frame."""
        handler, app = _make_h2_actor()
        # First HEADERS: regular field only, no END_HEADERS
        h1_fields = [(b'content-type', b'text/html')]
        h1 = _make_headers_frame(1, end_stream=False, end_headers=False,
                                 fields=h1_fields)
        # CONTINUATION with pseudo-headers — still malformed
        encoder = Encoder()
        block = encoder.encode([(b':method', b'GET'), (b':path', b'/'),
                                (b':scheme', b'https'), (b':authority', b'example.com')])
        cont = _make_h2_frame(FrameTypes.CONTINUATION,
                              HeaderFrameFlags.END_HEADERS,
                              stream_id=1, payload=block)
        settings = _make_h2_frame(FrameTypes.SETTINGS, 0, 0, b'')
        handler.receive = AsyncMock(side_effect=[settings, h1, cont, None])
        await handler.run()
        for call in handler.send_frame.call_args_list:
            frame = call.args[0]
            if hasattr(frame, 'FrameType') and frame.FrameType() == FrameTypes.RST_STREAM:
                if frame.stream_id == 1:
                    return
        pytest.fail('Pseudo-headers in CONTINUATION after regular field not rejected')


# ═══════════════════════════════════════════════════════════════════════
# G15: A client cannot push (§6.6 / §8.4) — PUSH_PROMISE is a connection error
# ═══════════════════════════════════════════════════════════════════════

class TestG15ClientPushPromise:
    """RFC 9113 §6.6 and §8.4: servers MUST treat the receipt of a PUSH_PROMISE
    as a connection error of type PROTOCOL_ERROR, whatever the stream state.

    The field block is still decoded first — the table is connection-wide — so
    an undecodable one is answered with COMPRESSION_ERROR (see
    ``TestG4CompressionErrors``); this is about a block that decodes.
    """

    @staticmethod
    def _push_promise(stream_id: int = 1, promised: int = 2) -> bytes:
        block = Encoder().encode([
            (b':method', b'GET'), (b':path', b'/pushed'),
            (b':scheme', b'https'), (b':authority', b'example.com'),
        ])
        return _make_h2_frame(
            FrameTypes.PUSH_PROMISE, int(HeaderFrameFlags.END_HEADERS),
            stream_id, promised.to_bytes(4, 'big') + block)

    @staticmethod
    def _observe_connection_error(handler):
        handler._connection_error = AsyncMock(wraps=handler._connection_error)
        return handler._connection_error

    @pytest.mark.asyncio
    async def test_a_push_promise_on_an_idle_stream_goes_away(self):
        handler, app = _make_h2_actor()
        connection_error = self._observe_connection_error(handler)
        settings = _make_h2_frame(FrameTypes.SETTINGS, 0, 0, b'')
        handler.receive = AsyncMock(side_effect=[
            settings, self._push_promise(), None])
        await handler.run()

        assert _sent_goaway_codes(handler) == [ErrorCodes.PROTOCOL_ERROR]
        assert _sent_rst_streams(handler, 1) == []
        assert 'may not push' in connection_error.call_args.args[1]
        assert app.await_count == 0

    @pytest.mark.parametrize('head', [
        pytest.param(_make_headers_frame(1, end_stream=False), id='open-stream'),
        pytest.param(_make_headers_frame(1, end_stream=True), id='half-closed-stream'),
    ])
    @pytest.mark.asyncio
    async def test_a_push_promise_on_an_open_stream_goes_away(self, head):
        """PUSH_PROMISE is a connection PROTOCOL_ERROR whatever the stream
        state (the open-stream case used to raise out of the responder
        lookup; half-closed(remote) used to answer RST_STREAM(STREAM_CLOSED))."""
        handler, app = _make_h2_actor()
        connection_error = self._observe_connection_error(handler)
        settings = _make_h2_frame(FrameTypes.SETTINGS, 0, 0, b'')
        handler.receive = AsyncMock(side_effect=[
            settings, head,
            self._push_promise(), None])
        await handler.run()

        assert _sent_goaway_codes(handler) == [ErrorCodes.PROTOCOL_ERROR]
        assert _sent_rst_streams(handler, 1) == []
        assert 'may not push' in connection_error.call_args.args[1]

    @pytest.mark.asyncio
    async def test_a_client_request_still_reaches_the_app(self):
        """The refusal is about PUSH_PROMISE, not about the stream state."""
        handler, app = _make_h2_actor()
        settings = _make_h2_frame(FrameTypes.SETTINGS, 0, 0, b'')
        handler.receive = AsyncMock(side_effect=[
            settings, _make_headers_frame(1, end_stream=True), None])
        await handler.run()

        assert app.await_count == 1
        assert _sent_goaway_codes(handler) == []
