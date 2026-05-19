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
    FrameTypes, FrameFlags, HeaderFrameFlags, DataFrameFlags,
    SettingFrameFlags, PseudoHeaders,
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
    block = encoder.encode(headers)
    flags = HeaderFrameFlags.END_HEADERS
    if end_stream:
        flags |= HeaderFrameFlags.END_STREAM
    return _make_raw_frame(FrameTypes.HEADERS, flags, stream_id, block)


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

    async def test_scope_type_is_websocket(self):
        captured = {}

        async def app(scope, receive, send):
            captured['type'] = scope['type']
            await receive()          # websocket.connect
            await send({'type': 'websocket.accept'})
            await receive()          # websocket.disconnect (from EOF)

        await self._run_with_app(app)
        assert captured['type'] == 'websocket'

    async def test_scope_http_version_is_2(self):
        captured = {}

        async def app(scope, receive, send):
            captured['http_version'] = scope['http_version']
            await receive()
            await send({'type': 'websocket.accept'})
            await receive()

        await self._run_with_app(app)
        assert captured['http_version'] == '2'

    async def test_scope_path(self):
        captured = {}

        async def app(scope, receive, send):
            captured['path'] = scope['path']
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

    async def test_scope_scheme_wss_for_https(self):
        captured = {}

        async def app(scope, receive, send):
            captured['scheme'] = scope['scheme']
            await receive()
            await send({'type': 'websocket.accept'})
            await receive()

        handler, _, _ = _make_h2_actor(app)
        frames = [_client_settings(),
                  _make_extended_connect_frame(scheme='https'),
                  None]
        handler.receive = AsyncMock(side_effect=frames)
        await handler.run()
        assert captured['scheme'] == 'wss'

    async def test_scope_scheme_ws_for_http(self):
        captured = {}

        async def app(scope, receive, send):
            captured['scheme'] = scope['scheme']
            await receive()
            await send({'type': 'websocket.accept'})
            await receive()

        handler, _, _ = _make_h2_actor(app)
        frames = [_client_settings(),
                  _make_extended_connect_frame(scheme='http'),
                  None]
        handler.receive = AsyncMock(side_effect=frames)
        await handler.run()
        assert captured['scheme'] == 'ws'

    async def test_scope_subprotocols_parsed(self):
        captured = {}

        async def app(scope, receive, send):
            captured['subprotocols'] = scope.get('subprotocols', [])
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
        from hpack import Decoder

        writes = []

        async def app(scope, receive, send):
            await receive()
            await send({'type': 'websocket.accept'})
            await receive()

        handler, _, writer = _make_h2_actor(app)
        writer.write = MagicMock(side_effect=lambda b: writes.append(b))
        writer.drain = AsyncMock()
        frames = [_client_settings(), _make_extended_connect_frame(), None]
        handler.receive = AsyncMock(side_effect=frames)
        await handler.run()

        # Find the HEADERS frame written to the wire
        all_bytes = b''.join(writes)
        # Scan for a HEADERS frame (type=0x01) containing :status 200
        found_200 = False
        decoder = Decoder()
        i = 0
        while i + 9 <= len(all_bytes):
            length = int.from_bytes(all_bytes[i:i+3], 'big')
            frame_type = all_bytes[i+3]
            payload = all_bytes[i+9:i+9+length]
            if frame_type == 0x01 and length > 0:  # HEADERS
                try:
                    hdrs = decoder.decode(payload)
                    for k, v in hdrs:
                        k = k.decode() if isinstance(k, bytes) else k
                        v = v.decode() if isinstance(v, bytes) else v
                        if k == ':status' and v == '200':
                            found_200 = True
                except Exception:
                    pass
            i += 9 + length
        assert found_200, 'Expected :status 200 HEADERS frame on websocket.accept'

    async def test_subprotocol_included_in_200_headers(self):
        """websocket.accept(subprotocol='chat') must include sec-websocket-protocol
        in the 200 response headers."""
        from hpack import Decoder

        writes = []

        async def app(scope, receive, send):
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

        all_bytes = b''.join(writes)
        found_subprotocol = False
        decoder = Decoder()
        i = 0
        while i + 9 <= len(all_bytes):
            length = int.from_bytes(all_bytes[i:i+3], 'big')
            frame_type = all_bytes[i+3]
            payload = all_bytes[i+9:i+9+length]
            if frame_type == 0x01 and length > 0:
                try:
                    hdrs = decoder.decode(payload)
                    for k, v in hdrs:
                        k = k.decode() if isinstance(k, bytes) else k
                        v = v.decode() if isinstance(v, bytes) else v
                        if k == 'sec-websocket-protocol' and v == 'chat':
                            found_subprotocol = True
                except Exception:
                    pass
            i += 9 + length
        assert found_subprotocol, (
            'Expected sec-websocket-protocol: chat in 200 response headers')


# ---------------------------------------------------------------------------
# §5 — Data exchange: WebSocket frames in HTTP/2 DATA frames
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestDataExchange:
    """RFC 6455 frames carried in HTTP/2 DATA frames after the handshake."""

    async def test_text_message_received_by_app(self):
        """A masked RFC 6455 TEXT frame in a DATA frame must arrive as
        websocket.receive with text field."""
        received = {}
        done = asyncio.Event()

        async def app(scope, receive, send):
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

        async def app(scope, receive, send):
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

        async def app(scope, receive, send):
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

    async def test_app_send_text_produces_data_frame(self):
        """websocket.send(text='hi') must result in DATA frame bytes on the wire
        containing a valid RFC 6455 TEXT frame payload."""
        from blackbull.server.ws_codec import read_frame_header, read_payload

        writes = []

        async def app(scope, receive, send):
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

    async def test_connect_without_protocol_not_websocket(self):
        """Plain CONNECT (no :protocol) must not produce a websocket scope."""
        scopes = []

        async def app(scope, receive, send):
            scopes.append(scope.get('type'))
            # Regular HTTP CONNECT — just reply
            await send({'type': 'http.response.start', 'status': 200, 'headers': []})
            await send({'type': 'http.response.body', 'body': b'', 'more_body': False})

        handler, _, _ = _make_h2_actor(app)
        handler.receive = AsyncMock(side_effect=[
            _client_settings(),
            _make_normal_connect_frame(),
            None,
        ])
        await handler.run()

        assert all(t != 'websocket' for t in scopes), (
            f'CONNECT without :protocol must not produce websocket scope; got {scopes}')

    async def test_connect_wrong_protocol_not_websocket(self):
        """CONNECT + :protocol=ftp must not produce a websocket scope."""
        scopes = []

        async def app(scope, receive, send):
            scopes.append(scope.get('type'))
            await send({'type': 'http.response.start', 'status': 400, 'headers': []})
            await send({'type': 'http.response.body', 'body': b'', 'more_body': False})

        handler, _, _ = _make_h2_actor(app)
        handler.receive = AsyncMock(side_effect=[
            _client_settings(),
            _make_connect_wrong_protocol_frame(),
            None,
        ])
        await handler.run()

        assert all(t != 'websocket' for t in scopes), (
            f'CONNECT with :protocol=ftp must not produce websocket scope; got {scopes}')

    async def test_multiple_ws_streams_multiplexed(self):
        """Two concurrent WebSocket streams on the same connection must each
        receive their own scope and messages independently."""
        scopes = []

        async def app(scope, receive, send):
            scopes.append(scope.get('path'))
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

        async def app(scope, receive, send):
            called.append(scope)

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
