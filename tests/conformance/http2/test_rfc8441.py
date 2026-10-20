"""RFC 8441 compliance tests — WebSocket Bootstrapping over HTTP/2 (Extended CONNECT).

Tests drive HTTP2Actor end-to-end using in-process fakes (no live sockets)
via the same pattern as test_http2_dispatch.py.

All tests involving the ASGI app run inside HTTP2Actor's TaskGroup.
"""
import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock
from hpack import Encoder

from blackbull.server.http2_actor import HTTP2Actor
from blackbull.server.sender import AsyncioWriter
from blackbull.protocol.frame import FrameFactory
from blackbull.protocol.frame_types import (
    FrameTypes, HeaderFrameFlags, DataFrameFlags,
    SettingFrameFlags,
)
from blackbull.server.ws_codec import encode_frame, WSOpcode


# RFC 8441 support is opt-in (BB_H2_ENABLE_WEBSOCKET).  These tests exercise
# the path, so enable it for every test in this module.
@pytest.fixture(autouse=True)
def _enable_ws_over_h2(monkeypatch):
    monkeypatch.setenv('BB_H2_ENABLE_WEBSOCKET', '1')


# ---------------------------------------------------------------------------
# Wire-format helpers
# ---------------------------------------------------------------------------

def _make_raw_frame(type_byte, flags, stream_id: int, payload: bytes) -> bytes:
    length = len(payload)
    return (length.to_bytes(3, 'big')
            + (type_byte if isinstance(type_byte, bytes) else bytes([type_byte]))
            + bytes([flags])
            + stream_id.to_bytes(4, 'big')
            + payload)


def _client_settings() -> bytes:
    """Empty SETTINGS from client (ACK-pending)."""
    return _make_raw_frame(FrameTypes.SETTINGS, SettingFrameFlags.INIT, 0, b'')


def _make_extended_connect_frame(
    path: str = '/ws',
    scheme: str = 'https',
    stream_id: int = 1,
    subprotocols: str = '',
    extensions: str = '',
    end_stream: bool = False,
) -> bytes:
    """HEADERS frame for RFC 8441 Extended CONNECT."""
    encoder = Encoder()
    headers = [
        (b':method', b'CONNECT'),
        (b':protocol', b'websocket'),
        (b':scheme', scheme.encode()),
        (b':path', path.encode()),
        (b':authority', b'localhost'),
    ]
    if subprotocols:
        headers.append((b'sec-websocket-protocol', subprotocols.encode()))
    if extensions:
        headers.append((b'sec-websocket-extensions', extensions.encode()))
    block = encoder.encode(headers)
    flags = HeaderFrameFlags.END_HEADERS
    if end_stream:
        flags |= HeaderFrameFlags.END_STREAM
    return _make_raw_frame(FrameTypes.HEADERS, flags, stream_id, block)


def _make_split_extended_connect_frames(
    path: str = '/ws',
    stream_id: int = 1,
) -> tuple[bytes, bytes]:
    """RFC 8441 Extended CONNECT split across HEADERS and CONTINUATION."""
    block = Encoder().encode([
        (b':method', b'CONNECT'),
        (b':protocol', b'websocket'),
        (b':scheme', b'https'),
        (b':path', path.encode()),
        (b':authority', b'localhost'),
    ])
    split = len(block) // 2
    return (
        _make_raw_frame(FrameTypes.HEADERS, 0, stream_id, block[:split]),
        _make_raw_frame(
            FrameTypes.CONTINUATION,
            HeaderFrameFlags.END_HEADERS,
            stream_id,
            block[split:],
        ),
    )


def _make_normal_connect_frame(stream_id: int = 1) -> bytes:
    """Standard CONNECT (no :protocol) — should NOT trigger WebSocket path."""
    encoder = Encoder()
    block = encoder.encode([
        (b':method', b'CONNECT'),
        (b':authority', b'example.com:443'),
    ])
    return _make_raw_frame(FrameTypes.HEADERS, HeaderFrameFlags.END_HEADERS,
                           stream_id, block)


def _make_connect_wrong_protocol_frame(stream_id: int = 1) -> bytes:
    """CONNECT + :protocol=ftp — should NOT trigger WebSocket path."""
    encoder = Encoder()
    block = encoder.encode([
        (b':method', b'CONNECT'),
        (b':protocol', b'ftp'),
        (b':scheme', b'https'),
        (b':path', b'/'),
        (b':authority', b'localhost'),
    ])
    return _make_raw_frame(FrameTypes.HEADERS, HeaderFrameFlags.END_HEADERS,
                           stream_id, block)


def _make_ws_data_frame(ws_frame_bytes: bytes, stream_id: int = 1,
                        end_stream: bool = False) -> bytes:
    """DATA frame wrapping raw RFC 6455 WebSocket frame bytes."""
    flags = DataFrameFlags.END_STREAM if end_stream else 0
    return _make_raw_frame(FrameTypes.DATA, flags, stream_id, ws_frame_bytes)


def _response_headers(writes) -> list[tuple[str, str]]:
    """Every (name, value) pair from the HEADERS frames written to the wire.

    Strict on purpose: a decode failure must fail the test, never hide the
    headers the test is about to assert on.
    """
    from hpack import Decoder
    all_bytes = b''.join(writes)
    out: list[tuple[str, str]] = []
    decoder = Decoder()
    i = 0
    while i + 9 <= len(all_bytes):
        length = int.from_bytes(all_bytes[i:i+3], 'big')
        if all_bytes[i+3] == 0x01 and length > 0:  # HEADERS
            for k, v in decoder.decode(all_bytes[i+9:i+9+length]):
                out.append((k.decode() if isinstance(k, bytes) else k,
                            v.decode() if isinstance(v, bytes) else v))
        i += 9 + length
    return out


def _echoed_ws_frame(writes) -> bytes:
    """The payload of the first DATA frame sent back to the client."""
    all_bytes = b''.join(writes)
    i = 0
    while i + 9 <= len(all_bytes):
        length = int.from_bytes(all_bytes[i:i+3], 'big')
        if all_bytes[i+3] == 0x00 and length > 0 and all_bytes[i+4] & 0x01 == 0:
            return all_bytes[i+9:i+9+length]
        i += 9 + length
    raise AssertionError('no DATA frame echoed')


def _make_h2_actor(app=None):
    if app is None:
        app = AsyncMock()
    writer = MagicMock()
    writer.drain = AsyncMock()
    writer.close = AsyncMock()
    handler = HTTP2Actor(None, AsyncioWriter(writer), app, aggregator=None)
    handler.send_frame = AsyncMock()
    return handler, app, writer


# ---------------------------------------------------------------------------
# §3 — SETTINGS_ENABLE_CONNECT_PROTOCOL advertisement
# ---------------------------------------------------------------------------

class TestSettingsAdvertisement:
    def test_settings_frame_payload_contains_0x0008(self):
        """FrameFactory.settings(enable_connect_protocol=True) must produce
        a SETTINGS frame whose payload encodes 0x0008 = 1."""
        factory = FrameFactory()
        frame = factory.settings(enable_connect_protocol=True)
        data = frame.save()
        # 9-byte header + 6-byte param (0x0008 = 1)
        assert len(data) == 15, f'Expected 15 bytes, got {len(data)}'
        assert data[9:15] == b'\x00\x08\x00\x00\x00\x01'

    def test_settings_ack_does_not_include_enable_connect(self):
        """ACK frames must carry no payload."""
        factory = FrameFactory()
        frame = factory.settings(ack=True, enable_connect_protocol=True)
        data = frame.save()
        assert len(data) == 9  # header only

    def test_settings_frame_parse_round_trip(self):
        """A SETTINGS frame produced by FrameFactory must be parseable back
        with enable_connect_protocol = 1."""
        factory = FrameFactory()
        raw = factory.settings(enable_connect_protocol=True).save()
        loaded = factory.load(raw)
        assert hasattr(loaded, 'enable_connect_protocol'), (
            'Parsed SETTINGS must expose enable_connect_protocol attribute')
        assert loaded.enable_connect_protocol == 1

    @pytest.mark.asyncio
    async def test_actor_sends_enable_connect_in_initial_settings(self):
        """HTTP2Actor.run() must advertise SETTINGS_ENABLE_CONNECT_PROTOCOL=1
        as its very first send_frame call."""
        handler, _, _ = _make_h2_actor()
        handler.receive = AsyncMock(return_value=None)  # immediate EOF
        await handler.run()

        assert handler.send_frame.call_count >= 1
        first_frame = handler.send_frame.call_args_list[0].args[0]
        assert first_frame.FrameType() == FrameTypes.SETTINGS
        raw = first_frame.save()
        assert b'\x00\x08\x00\x00\x00\x01' in raw, (
            'Initial SETTINGS must contain SETTINGS_ENABLE_CONNECT_PROTOCOL=1')


# ---------------------------------------------------------------------------
# §4 — Extended CONNECT handshake and ASGI scope
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestExtendedConnectHandshake:
    """Extended CONNECT HEADERS → WebSocket scope + deferred 200 response."""

    async def _run_with_app(self, app, extra_frames=None):
        """Run HTTP2Actor with a CONNECT HEADERS frame and optional extra frames."""
        handler, _, writer = _make_h2_actor(app)
        frames = [_client_settings(), _make_extended_connect_frame()]
        if extra_frames:
            frames.extend(extra_frames)
        frames.append(None)
        handler.receive = AsyncMock(side_effect=frames)
        await handler.run()
        return writer

    @pytest.mark.parametrize('attr,expected', [
        pytest.param('type', 'websocket', id='scope-type-websocket'),
        pytest.param('http_version', '2', id='scope-http-version-2'),
    ])
    async def test_scope_type_is_websocket(self, attr, expected):
        captured = {}

        async def app(conn, receive, send):
            captured[attr] = getattr(conn, attr)
            await receive()          # websocket.connect
            await send({'type': 'websocket.accept'})
            await receive()          # websocket.disconnect (from EOF)

        await self._run_with_app(app)
        assert captured[attr] == expected

    async def test_split_headers_use_the_same_websocket_lifecycle(self):
        captured = {}

        async def app(conn, receive, send):
            captured['type'] = conn.type
            captured['first_event'] = await receive()
            await send({'type': 'websocket.accept'})
            captured['last_event'] = await receive()

        handler, _, _ = _make_h2_actor(app)
        headers, continuation = _make_split_extended_connect_frames()
        handler.receive = AsyncMock(side_effect=[
            _client_settings(), headers, continuation, None,
        ])

        await handler.run()

        assert captured['type'] == 'websocket'
        assert captured['first_event'] == {'type': 'websocket.connect'}
        assert captured['last_event']['type'] == 'websocket.disconnect'

    async def test_scope_path(self):
        captured = {}

        async def app(conn, receive, send):
            captured['path'] = conn.path
            await receive()
            await send({'type': 'websocket.accept'})
            await receive()

        handler, _, _ = _make_h2_actor(app)
        frames = [_client_settings(),
                  _make_extended_connect_frame(path='/chat-room'),
                  None]
        handler.receive = AsyncMock(side_effect=frames)
        await handler.run()
        assert captured['path'] == '/chat-room'

    @pytest.mark.parametrize('scheme,expected', [
        pytest.param('https', 'wss', id='https-to-wss'),
        pytest.param('HTTPS', 'wss', id='HTTPS-to-wss'),
        pytest.param('http', 'ws', id='http-to-ws'),
        pytest.param('HTTP', 'ws', id='HTTP-to-ws'),
    ])
    async def test_scope_scheme_wss_for_https(self, scheme, expected):
        """RFC8441 scope scheme mapping: https → wss, http → ws,
        case-insensitively."""
        captured = {}

        async def app(conn, receive, send):
            captured['scheme'] = conn.scheme
            await receive()
            await send({'type': 'websocket.accept'})
            await receive()

        handler, _, _ = _make_h2_actor(app)
        frames = [_client_settings(),
                  _make_extended_connect_frame(scheme=scheme),
                  None]
        handler.receive = AsyncMock(side_effect=frames)
        await handler.run()
        assert captured['scheme'] == expected

    async def test_scope_subprotocols_parsed(self):
        captured = {}

        async def app(conn, receive, send):
            captured['subprotocols'] = conn.subprotocols
            await receive()
            await send({'type': 'websocket.accept'})
            await receive()

        handler, _, _ = _make_h2_actor(app)
        frames = [_client_settings(),
                  _make_extended_connect_frame(subprotocols='chat, graphql'),
                  None]
        handler.receive = AsyncMock(side_effect=frames)
        await handler.run()
        assert 'chat' in captured['subprotocols']
        assert 'graphql' in captured['subprotocols']

    async def test_accept_sends_200_response(self):
        """websocket.accept must trigger a :status 200 HEADERS frame (not 101)."""

        writes = []

        async def app(conn, receive, send):
            await receive()
            await send({'type': 'websocket.accept'})
            await receive()

        handler, _, writer = _make_h2_actor(app)
        writer.write = MagicMock(side_effect=lambda b: writes.append(b))
        writer.drain = AsyncMock()
        frames = [_client_settings(), _make_extended_connect_frame(), None]
        handler.receive = AsyncMock(side_effect=frames)
        await handler.run()

        assert (':status', '200') in _response_headers(writes), \
            'Expected :status 200 HEADERS frame on websocket.accept'

    async def test_subprotocol_included_in_200_headers(self):
        """websocket.accept(subprotocol='chat') must include sec-websocket-protocol
        in the 200 response headers."""

        writes = []

        async def app(conn, receive, send):
            await receive()
            await send({'type': 'websocket.accept', 'subprotocol': 'chat'})
            await receive()

        handler, _, writer = _make_h2_actor(app)
        writer.write = MagicMock(side_effect=lambda b: writes.append(b))
        writer.drain = AsyncMock()
        frames = [_client_settings(),
                  _make_extended_connect_frame(subprotocols='chat'),
                  None]
        handler.receive = AsyncMock(side_effect=frames)
        await handler.run()

        assert (':status', '200') in _response_headers(writes)
        assert ('sec-websocket-protocol', 'chat') in _response_headers(writes), (
            'Expected sec-websocket-protocol: chat in 200 response headers')


# ---------------------------------------------------------------------------
# §5 — Data exchange: WebSocket frames in HTTP/2 DATA frames
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestPermessageDeflateNegotiation:
    """RFC 7692 negotiation over the RFC 8441 handshake (BLA-362)."""

    @pytest.mark.parametrize('extensions, expect_extension, compressed', [
        ('permessage-deflate', True, True),
        ('permessage-deflate; server_max_window_bits=8', False, False),
        ('', False, False),
    ])
    async def test_the_200_answers_the_offer_and_the_round_trip_works(
            self, extensions, expect_extension, compressed):
        """The 200 answers the offer exactly as H1's 101 would — an
        accepted offer echoes the extension line and the round trip
        carries RFC 7692 frames; a declined or absent offer answers
        plain and the plain round trip keeps working."""
        from blackbull.server.permessage_deflate import (
            InboundDecompressor, OutboundCompressor, negotiate)
        received = {}

        async def app(conn, receive, send):
            await receive()
            await send({'type': 'websocket.accept'})
            event = await receive()
            received.update(event)
            await send({'type': 'websocket.send', 'text': event['text']})
            await receive()

        params = None
        if compressed:
            params, _ = negotiate(b'permessage-deflate')
            assert params is not None, 'this runtime must serve the bare offer'
            payload = OutboundCompressor(
                wbits=params.client_max_window_bits,
                reset_per_message=params.client_no_context_takeover,
            ).compress(b'hello')
            ws_bytes = encode_frame(payload, opcode=WSOpcode.TEXT,
                                    mask=True, rsv1=True)
        else:
            ws_bytes = encode_frame(b'hello', opcode=WSOpcode.TEXT, mask=True)

        handler, _, writer = _make_h2_actor(app)
        writes = []
        writer.write = MagicMock(side_effect=lambda b: writes.append(b))
        writer.drain = AsyncMock()
        handler.receive = AsyncMock(side_effect=[
            _client_settings(),
            _make_extended_connect_frame(extensions=extensions),
            _make_ws_data_frame(ws_bytes, stream_id=1),
            None,
        ])
        await handler.run()

        headers = _response_headers(writes)
        assert (':status', '200') in headers, headers
        assert any(k == 'sec-websocket-extensions'
                   and v.startswith('permessage-deflate')
                   for k, v in headers) == expect_extension, headers
        assert received.get('text') == 'hello'
        echoed = _echoed_ws_frame(writes)
        if compressed:
            assert echoed[0] & 0x40, 'server echo must be RFC 7692 compressed'
            out = InboundDecompressor(
                wbits=params.server_max_window_bits,
                reset_per_message=params.server_no_context_takeover,
            ).decompress(echoed[2:])
            assert out == b'hello'
        else:
            assert not echoed[0] & 0x40, 'a declined offer must echo plain'
            assert echoed[2:] == b'hello'


@pytest.mark.asyncio
class TestDataExchange:
    """RFC 6455 frames carried in HTTP/2 DATA frames after the handshake."""

    async def test_text_message_received_by_app(self):
        """A masked RFC 6455 TEXT frame in a DATA frame must arrive as
        websocket.receive with text field."""
        received = {}
        done = asyncio.Event()

        async def app(conn, receive, send):
            await receive()          # websocket.connect
            await send({'type': 'websocket.accept'})
            event = await receive()  # websocket.receive
            received.update(event)
            done.set()
            await receive()          # websocket.disconnect (EOF)

        ws_bytes = encode_frame(b'hello', opcode=WSOpcode.TEXT, mask=True)
        data_frame = _make_ws_data_frame(ws_bytes, stream_id=1)

        handler, _, _ = _make_h2_actor(app)
        handler.receive = AsyncMock(side_effect=[
            _client_settings(),
            _make_extended_connect_frame(),
            data_frame,
            None,
        ])
        await handler.run()

        assert received.get('type') == 'websocket.receive'
        assert received.get('text') == 'hello'

    async def test_binary_message_received_by_app(self):
        """A masked RFC 6455 BINARY frame must arrive as websocket.receive
        with bytes field."""
        received = {}

        async def app(conn, receive, send):
            await receive()
            await send({'type': 'websocket.accept'})
            event = await receive()
            received.update(event)
            await receive()

        payload = b'\x01\x02\x03'
        ws_bytes = encode_frame(payload, opcode=WSOpcode.BINARY, mask=True)
        data_frame = _make_ws_data_frame(ws_bytes, stream_id=1)

        handler, _, _ = _make_h2_actor(app)
        handler.receive = AsyncMock(side_effect=[
            _client_settings(),
            _make_extended_connect_frame(),
            data_frame,
            None,
        ])
        await handler.run()

        assert received.get('type') == 'websocket.receive'
        assert received.get('bytes') == payload

    async def test_end_stream_delivers_disconnect(self):
        """A DATA frame with END_STREAM flag must result in websocket.disconnect
        being returned from the app's receive()."""
        received_events = []

        async def app(conn, receive, send):
            await receive()              # websocket.connect
            await send({'type': 'websocket.accept'})
            event = await receive()      # websocket.disconnect (from END_STREAM)
            received_events.append(event)

        # END_STREAM in DATA frame signals orderly closure (RFC 8441 §5)
        empty_data_end = _make_ws_data_frame(b'', stream_id=1, end_stream=True)

        handler, _, _ = _make_h2_actor(app)
        handler.receive = AsyncMock(side_effect=[
            _client_settings(),
            _make_extended_connect_frame(),
            empty_data_end,
            None,
        ])
        await handler.run()

        assert any(e.get('type') == 'websocket.disconnect'
                   for e in received_events), (
            'END_STREAM DATA frame must deliver websocket.disconnect to the app')
        assert handler._stream_tasks == {}
        assert handler._senders == {}
        assert handler._recipients == {}
        assert handler.root_stream.children == {}
        assert handler._active_stream_count == 0
        assert handler._ws_stream_count == 0

    async def test_app_send_text_produces_data_frame(self):
        """websocket.send(text='hi') must result in DATA frame bytes on the wire
        containing a valid RFC 6455 TEXT frame payload."""

        writes = []

        async def app(conn, receive, send):
            await receive()
            await send({'type': 'websocket.accept'})
            await send({'type': 'websocket.send', 'text': 'hi'})
            await receive()

        handler, _, writer = _make_h2_actor(app)
        writer.write = MagicMock(side_effect=lambda b: writes.append(b))
        writer.drain = AsyncMock()
        handler.receive = AsyncMock(side_effect=[
            _client_settings(),
            _make_extended_connect_frame(),
            None,
        ])
        await handler.run()

        # Scan written bytes for a DATA frame (type=0x00)
        all_bytes = b''.join(writes)
        data_payloads = []
        i = 0
        while i + 9 <= len(all_bytes):
            length = int.from_bytes(all_bytes[i:i+3], 'big')
            frame_type = all_bytes[i+3]
            payload = all_bytes[i+9:i+9+length]
            if frame_type == 0x00 and length > 0:  # DATA with body
                data_payloads.append(payload)
            i += 9 + length

        # At least one DATA payload must decode as a text WebSocket frame with 'hi'
        found = False
        for dp in data_payloads:
            if len(dp) >= 2 and (dp[0] & 0x0F) == WSOpcode.TEXT:
                mask_bit = dp[1] & 0x80
                raw_len = dp[1] & 0x7F
                if not mask_bit and raw_len == len(b'hi'):
                    text = dp[2:2+raw_len]
                    if text == b'hi':
                        found = True
                        break
        assert found, f'Expected TEXT WebSocket frame with "hi" in DATA payloads; got {data_payloads!r}'


# ---------------------------------------------------------------------------
# §4 negative cases — non-WebSocket CONNECT must not enter WS path
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestNegativeCases:
    """CONNECT without :protocol or with wrong :protocol must not trigger RFC 8441."""

    @pytest.mark.parametrize('make_frame,reply_status,label', [
        pytest.param(_make_normal_connect_frame, 200, 'CONNECT without :protocol',
                     id='no-protocol'),
        pytest.param(_make_connect_wrong_protocol_frame, 400, 'CONNECT with :protocol=ftp',
                     id='wrong-protocol'),
    ])
    async def test_connect_without_protocol_not_websocket(self, make_frame, reply_status, label):
        """Extended CONNECT without or with a wrong :protocol must not
        produce a websocket scope."""
        scopes = []

        async def app(conn, receive, send):
            scopes.append(conn.type)
            await send({'type': 'http.response.start', 'status': reply_status, 'headers': []})
            await send({'type': 'http.response.body', 'body': b'', 'more_body': False})

        handler, _, _ = _make_h2_actor(app)
        handler.receive = AsyncMock(side_effect=[
            _client_settings(),
            make_frame(),
            None,
        ])
        await handler.run()

        assert all(t != 'websocket' for t in scopes), (
            f'{label} must not produce websocket scope; got {scopes}')

    async def test_multiple_ws_streams_multiplexed(self):
        """Two concurrent WebSocket streams on the same connection must each
        receive their own scope and messages independently."""
        scopes = []

        async def app(conn, receive, send):
            scopes.append(conn.path)
            await receive()          # websocket.connect
            await send({'type': 'websocket.accept'})
            await receive()          # disconnect (EOF)

        handler, _, _ = _make_h2_actor(app)
        handler.receive = AsyncMock(side_effect=[
            _client_settings(),
            _make_extended_connect_frame(path='/ws/a', stream_id=1),
            _make_extended_connect_frame(path='/ws/b', stream_id=3),
            None,
        ])
        await handler.run()

        assert len(scopes) == 2, f'Expected 2 WS streams; got scopes={scopes}'
        assert '/ws/a' in scopes
        assert '/ws/b' in scopes


@pytest.mark.asyncio
class TestOptInGating:
    """BB_H2_ENABLE_WEBSOCKET gates the entire RFC 8441 path."""

    async def test_settings_omits_enable_connect_when_disabled(self, monkeypatch):
        """With BB_H2_ENABLE_WEBSOCKET unset, the initial SETTINGS frame
        MUST NOT carry SETTINGS_ENABLE_CONNECT_PROTOCOL=1."""
        monkeypatch.setenv('BB_H2_ENABLE_WEBSOCKET', '0')
        handler, _, _ = _make_h2_actor()
        handler.receive = AsyncMock(return_value=None)
        await handler.run()

        first_frame = handler.send_frame.call_args_list[0].args[0]
        assert first_frame.FrameType() == FrameTypes.SETTINGS
        raw = first_frame.save()
        assert b'\x00\x08\x00\x00\x00\x01' not in raw, (
            'SETTINGS must not advertise ENABLE_CONNECT_PROTOCOL when disabled')

    async def test_extended_connect_rejected_with_rst_when_disabled(self, monkeypatch):
        """An Extended CONNECT request received while RFC 8441 support is off
        MUST be answered with RST_STREAM(PROTOCOL_ERROR); the app handler
        MUST NOT be invoked."""
        monkeypatch.setenv('BB_H2_ENABLE_WEBSOCKET', '0')
        called = []

        async def app(conn, receive, send):
            called.append(conn)

        handler, _, _ = _make_h2_actor(app)
        handler.receive = AsyncMock(side_effect=[
            _client_settings(),
            _make_extended_connect_frame(),
            None,
        ])
        await handler.run()

        assert called == [], 'app handler must not be invoked when WS-over-h2 is off'
        rst_seen = any(
            getattr(call.args[0], 'FrameType', lambda: None)() == FrameTypes.RST_STREAM
            for call in handler.send_frame.call_args_list
        )
        assert rst_seen, 'Server must send RST_STREAM in response to disallowed Extended CONNECT'


# ---------------------------------------------------------------------------
# §6 — BB_H2_WS_MAX_STREAMS_PER_CONNECTION stream-exhaustion guard
# ---------------------------------------------------------------------------

from blackbull.protocol.frame_types import ErrorCodes  # noqa: E402


def _rst_calls(handler) -> list:
    """Return (stream_id, error_code) tuples for every RST_STREAM frame
    the actor emitted via ``send_frame``."""
    out = []
    for call in handler.send_frame.call_args_list:
        f = call.args[0]
        if (hasattr(f, 'FrameType')
                and f.FrameType() == FrameTypes.RST_STREAM):
            out.append((f.stream_id, getattr(f, 'error_code', None)))
    return out


@pytest.mark.asyncio
class TestWebSocketStreamCap:
    """``BB_H2_WS_MAX_STREAMS_PER_CONNECTION`` caps the number of in-flight
    Extended CONNECT streams per HTTP/2 connection.  This defends against
    stream-exhaustion DoS — without the cap, an attacker can hold
    ``BB_H2_MAX_CONCURRENT_STREAMS`` (default 100) idle WS streams per
    connection across an unbounded ``BB_MAX_CONNECTIONS`` (default 0).

    Tests use a *blocking* ASGI app so the WS handler tasks stay alive
    while the receive loop processes subsequent Extended CONNECT frames;
    ``asyncio.wait_for`` then bounds the test and cancels still-pending
    WS tasks cleanly through the TaskGroup.
    """

    @staticmethod
    def _blocking_app():
        async def app(conn, receive, send):
            # Park forever — handler.run() will be cancelled by wait_for.
            await asyncio.Event().wait()
        return app

    async def test_ws_streams_within_cap_are_accepted(self, monkeypatch):
        """With cap=5, five Extended CONNECT streams must all be accepted
        — no RST_STREAM emitted."""
        monkeypatch.setenv('BB_H2_WS_MAX_STREAMS_PER_CONNECTION', '5')

        handler, _, _ = _make_h2_actor(app=self._blocking_app())
        handler.receive = AsyncMock(side_effect=[
            _client_settings(),
            *(_make_extended_connect_frame(stream_id=1 + 2 * i)
              for i in range(5)),
            None,
        ])
        try:
            await asyncio.wait_for(handler.run(), timeout=1.0)
        except (asyncio.TimeoutError, TimeoutError):
            # Expected — blocking app keeps WS tasks alive; cancel via timeout.
            pass

        rsts = _rst_calls(handler)
        assert rsts == [], (
            f'no streams should be refused within the cap; got RSTs={rsts}')

    async def test_ws_streams_exceeding_cap_are_refused(self, monkeypatch):
        """With cap=5, the 6th Extended CONNECT must receive
        ``RST_STREAM(REFUSED_STREAM)``."""
        monkeypatch.setenv('BB_H2_WS_MAX_STREAMS_PER_CONNECTION', '5')

        handler, _, _ = _make_h2_actor(app=self._blocking_app())
        # Streams 1, 3, 5, 7, 9 are within cap; stream 11 is the 6th.
        handler.receive = AsyncMock(side_effect=[
            _client_settings(),
            *(_make_extended_connect_frame(stream_id=1 + 2 * i)
              for i in range(6)),
            None,
        ])
        try:
            await asyncio.wait_for(handler.run(), timeout=1.0)
        except (asyncio.TimeoutError, TimeoutError):
            pass  # expected — receive iterator exhausted, run() spins until timeout

        rsts = _rst_calls(handler)
        refused = [(sid, ec) for sid, ec in rsts
                   if ec == ErrorCodes.REFUSED_STREAM]
        # Exactly the 6th stream (id 11) must be refused; the first 5 pass.
        assert refused == [(11, ErrorCodes.REFUSED_STREAM)], (
            f'the 6th Extended CONNECT must be RST_STREAM(REFUSED_STREAM); '
            f'got refused={refused}, all RSTs={rsts}')

    async def test_ws_stream_cap_is_per_connection(self, monkeypatch):
        """The cap is per-connection (per HTTP2Actor instance) — two
        independent connections each get their own quota."""
        monkeypatch.setenv('BB_H2_WS_MAX_STREAMS_PER_CONNECTION', '5')

        async def drive_connection() -> list:
            handler, _, _ = _make_h2_actor(app=self._blocking_app())
            handler.receive = AsyncMock(side_effect=[
                _client_settings(),
                *(_make_extended_connect_frame(stream_id=1 + 2 * i)
                  for i in range(5)),
                None,
            ])
            try:
                await asyncio.wait_for(handler.run(), timeout=1.0)
            except (asyncio.TimeoutError, TimeoutError):
                pass  # expected — receive iterator exhausted, run() spins until timeout
            return _rst_calls(handler)

        # Run two independent actors; both should accept 5 streams each.
        rsts_a, rsts_b = await asyncio.gather(
            drive_connection(), drive_connection())
        assert rsts_a == [] and rsts_b == [], (
            f'cap must be per-connection, not global; '
            f'actor A RSTs={rsts_a}, actor B RSTs={rsts_b}')

    async def test_cap_zero_disables_per_connection_limit(self, monkeypatch):
        """``BB_H2_WS_MAX_STREAMS_PER_CONNECTION=0`` disables the cap —
        well beyond 5 Extended CONNECTs must all be accepted (subject only
        to ``BB_H2_MAX_CONCURRENT_STREAMS``)."""
        monkeypatch.setenv('BB_H2_WS_MAX_STREAMS_PER_CONNECTION', '0')

        n = 20
        handler, _, _ = _make_h2_actor(app=self._blocking_app())
        handler.receive = AsyncMock(side_effect=[
            _client_settings(),
            *(_make_extended_connect_frame(stream_id=1 + 2 * i)
              for i in range(n)),
            None,
        ])
        try:
            await asyncio.wait_for(handler.run(), timeout=1.0)
        except (asyncio.TimeoutError, TimeoutError):
            pass  # expected — receive iterator exhausted, run() spins until timeout

        rsts = _rst_calls(handler)
        refused = [r for r in rsts if r[1] == ErrorCodes.REFUSED_STREAM]
        assert refused == [], (
            f'with cap=0, no Extended CONNECT should be REFUSED_STREAM; '
            f'got refused={refused}')


# ---------------------------------------------------------------------------
# §5 — a WebSocket stream holds at most one window unconsumed
# ---------------------------------------------------------------------------

from blackbull.server.http2_ws import HTTP2StreamReader  # noqa: E402
from blackbull.server.recipient import IncompleteReadError  # noqa: E402


@pytest.mark.asyncio
async def test_data_past_a_websocket_streams_window_is_reset():
    """RST_STREAM(ENHANCE_YOUR_CALM), and the codec parked on the stream wakes."""
    from blackbull.protocol.stream import Stream, StreamState  # noqa: PLC0415

    handler, _, _ = _make_h2_actor()
    handler.receive = AsyncMock(return_value=None)
    await handler.run()
    handler.send_frame.reset_mock()
    stream = Stream(stream_id=1, parent=handler.root_stream)
    stream.state = StreamState.OPEN
    handler.root_stream.add_child(1)

    async def credit(n):
        pass

    reader = HTTP2StreamReader(4, credit)
    handler._recipients[1] = reader
    await handler._on_data_frame(
        handler.factory.load(_make_ws_data_frame(b'payload-bytes')), stream)

    rsts = [c.args[0] for c in handler.send_frame.call_args_list
            if c.args[0].FrameType() == FrameTypes.RST_STREAM]
    assert [r.error_code for r in rsts] == [ErrorCodes.ENHANCE_YOUR_CALM]
    with pytest.raises(IncompleteReadError):
        await asyncio.wait_for(reader.readexactly(1), 1)


# ---------------------------------------------------------------------------
# Application accept headers on the 200 (BLA-378)
# ---------------------------------------------------------------------------

class TestAcceptHeadersOnThe200:
    def _headers_200(self, app, request_frame=None):

        writes = []

        handler, _, writer = _make_h2_actor(app)
        writer.write = MagicMock(side_effect=lambda b: writes.append(b))
        writer.drain = AsyncMock()
        frames = [_client_settings(),
                  request_frame or _make_extended_connect_frame(),
                  None]
        handler.receive = AsyncMock(side_effect=frames)
        asyncio.run(handler.run())

        norm = _response_headers(writes)
        return norm if (':status', '200') in norm else None

    def test_accept_headers_reach_the_200_in_order(self):
        async def app(conn, receive, send):
            await receive()
            await send({'type': 'websocket.accept', 'subprotocol': None,
                        'headers': [[b'set-cookie', b'a=1'],
                                    [b'x-mid', b'm'],
                                    [b'set-cookie', b'b=2']]})
            await receive()

        found = self._headers_200(app)
        assert found is not None
        want = [('set-cookie', 'a=1'), ('x-mid', 'm'), ('set-cookie', 'b=2')]
        assert [kv for kv in found if kv in want] == want

    def test_a_native_accept_header_reaches_the_200(self):
        from blackbull.native import NativeWSMessage

        async def app(conn, receive, send):
            await receive()
            await send(NativeWSMessage.accept(None, [(b'x-review', b'ok')]))
            await receive()

        found = self._headers_200(app)
        assert found is not None
        assert ('x-review', 'ok') in found

    def test_an_invalid_header_value_is_refused(self):
        async def app(conn, receive, send):
            await receive()
            await send({'type': 'websocket.accept', 'subprotocol': None,
                        'headers': [[b'x-review', b'ok\r\ninjected: 1']]})
            await receive()

        assert self._headers_200(app) is None

    def test_a_reserved_protocol_header_is_refused(self):
        async def app(conn, receive, send):
            await receive()
            await send({'type': 'websocket.accept', 'subprotocol': None,
                        'headers': [[b'sec-websocket-accept', b'evil']]})
            await receive()

        assert self._headers_200(app) is None
