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

        async def app(conn, receive, send):
            captured['type'] = conn.type
            await receive()          # websocket.connect
            await send({'type': 'websocket.accept'})
            await receive()          # websocket.disconnect (from EOF)

        await self._run_with_app(app)
        assert captured['type'] == 'websocket'

    async def test_scope_http_version_is_2(self):
        captured = {}

        async def app(conn, receive, send):
            captured['http_version'] = conn.http_version
            await receive()
            await send({'type': 'websocket.accept'})
            await receive()

        await self._run_with_app(app)
        assert captured['http_version'] == '2'

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

    async def test_scope_scheme_wss_for_https(self):
        captured = {}

        async def app(conn, receive, send):
            captured['scheme'] = conn.scheme
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

        async def app(conn, receive, send):
            captured['scheme'] = conn.scheme
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
        from hpack import Decoder

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

    async def test_app_send_text_produces_data_frame(self):
        """websocket.send(text='hi') must result in DATA frame bytes on the wire
        containing a valid RFC 6455 TEXT frame payload."""
        from blackbull.server.ws_codec import read_frame_header, read_payload

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

    async def test_connect_without_protocol_not_websocket(self):
        """Plain CONNECT (no :protocol) must not produce a websocket scope."""
        scopes = []

        async def app(conn, receive, send):
            scopes.append(conn.type)
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

        async def app(conn, receive, send):
            scopes.append(conn.type)
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
# §7 — HTTP2WSReader buffer backpressure
# ---------------------------------------------------------------------------
#
# Without a buffer cap, an overloaded or misbehaving peer could grow
# HTTP2WSReader._buffer without bound — the reader credited every
# incoming DATA frame's window even when the actor wasn't draining.
# Improvement A caps the buffer at ``max_buffer`` (default 1 MiB) and
# switches credit emission to a credit-on-drain model when the cap is
# exceeded: bytes are still buffered (no silent data loss), but
# WINDOW_UPDATE is withheld until ``readexactly`` drains back below
# the cap.

from blackbull.protocol.frame_types import Data as _DataFrame  # noqa: E402
from blackbull.server.http2_ws import HTTP2WSReader  # noqa: E402


def _fake_data(payload: bytes, end_stream: bool = False) -> _DataFrame:
    """Real Data frame for HTTP2WSReader unit tests.  Beartype enforces
    the ``frame: Data`` annotation on ``put_DATAFrame`` at runtime, so
    a duck-typed stand-in (e.g. ``SimpleNamespace``) is rejected."""
    flags = int(DataFrameFlags.END_STREAM) if end_stream else 0
    return _DataFrame(
        len(payload), FrameTypes.DATA, flags, stream_id=1, data=payload)


@pytest.mark.asyncio
class TestHTTP2WSReaderBackpressure:
    """The reader buffers bytes always (so the peer's
    already-on-the-wire payload never disappears) and signals
    backpressure by withholding WINDOW_UPDATE.  Withheld credit is
    replayed when ``readexactly`` drains the buffer below ``max_buffer``.
    """

    async def test_returns_true_while_under_cap(self):
        r = HTTP2WSReader(max_buffer=100)
        assert r.put_DATAFrame(_fake_data(b'a' * 50)) is True
        assert r.put_DATAFrame(_fake_data(b'b' * 40)) is True
        assert r._pending_credit == 0

    async def test_returns_false_when_cap_exceeded_and_keeps_bytes(self):
        """Past the cap, ``put_DATAFrame`` returns ``False`` to signal
        the actor to skip WINDOW_UPDATE — but the bytes are still in
        the buffer (the peer already delivered them; dropping would
        violate WebSocket's reliable-delivery contract)."""
        r = HTTP2WSReader(max_buffer=100)
        assert r.put_DATAFrame(_fake_data(b'a' * 80)) is True
        # This one overflows: 80 + 30 = 110 > 100
        assert r.put_DATAFrame(_fake_data(b'b' * 30)) is False
        # Bytes still buffered
        assert len(r._buffer) == 110
        assert r._pending_credit == 30, (
            f'withheld credit must equal the overflowing frame size; '
            f'got {r._pending_credit}')

    async def test_pending_credit_accumulates_across_multiple_overflows(self):
        r = HTTP2WSReader(max_buffer=100)
        r.put_DATAFrame(_fake_data(b'x' * 80))   # under, no withhold
        r.put_DATAFrame(_fake_data(b'x' * 30))   # 110, withhold 30
        r.put_DATAFrame(_fake_data(b'x' * 20))   # 130, withhold 20 more
        assert r._pending_credit == 50

    async def test_credit_replay_fires_after_drain_below_cap(self):
        """Once the actor drains the buffer back under ``max_buffer``,
        the reader must invoke ``credit_callback`` with the accumulated
        withheld credit so the peer's WINDOW reopens."""
        credits: list[int] = []

        async def credit_cb(n: int) -> None:
            credits.append(n)

        r = HTTP2WSReader(max_buffer=100, credit_callback=credit_cb)
        # Fill to 110, withholding 30
        r.put_DATAFrame(_fake_data(b'a' * 80))
        r.put_DATAFrame(_fake_data(b'b' * 30))
        assert credits == [], 'no credit while over cap'

        # Drain 20 bytes — still 90 in buffer, under cap → replay
        drained = await r.readexactly(20)
        assert len(drained) == 20
        assert credits == [30], (
            f'credit_callback must replay withheld credit on drain; '
            f'got {credits}')
        assert r._pending_credit == 0

    async def test_no_replay_when_still_over_cap_after_drain(self):
        """A partial drain that leaves the buffer still over the cap
        must NOT replay credit — the peer should remain throttled
        until enough bytes are consumed."""
        credits: list[int] = []

        async def credit_cb(n: int) -> None:
            credits.append(n)

        r = HTTP2WSReader(max_buffer=100, credit_callback=credit_cb)
        r.put_DATAFrame(_fake_data(b'a' * 80))
        r.put_DATAFrame(_fake_data(b'b' * 80))    # 160, withhold 80
        assert r._pending_credit == 80

        # Drain only 10 — still 150 in buffer, still over cap
        await r.readexactly(10)
        assert credits == [], (
            f'credit must stay withheld while buffer remains over cap; '
            f'got {credits}')
        assert r._pending_credit == 80

        # Drain enough to go under cap (150 → 50)
        await r.readexactly(100)
        assert credits == [80]
        assert r._pending_credit == 0

    async def test_pending_credit_observable_without_callback(self):
        """When constructed without a callback (unit-test mode), the
        reader still tracks ``_pending_credit`` — useful for asserting
        the withhold behaviour.  The reset is gated on the callback
        firing successfully, so without a callback the count stays
        accumulated; production always wires a callback so this only
        matters for direct unit tests."""
        r = HTTP2WSReader(max_buffer=50)
        r.put_DATAFrame(_fake_data(b'x' * 80))
        assert r._pending_credit == 80
        await r.readexactly(80)
        assert r._pending_credit == 80, (
            'without a callback the reader has no peer to credit; '
            'pending_credit stays observable')

    async def test_default_max_buffer_is_1_mib(self):
        """The class-level default cap is 1 MiB — covers a single
        maximum-size WebSocket frame plus headroom."""
        r = HTTP2WSReader()
        assert r._max_buffer == 1024 * 1024


@pytest.mark.asyncio
class TestActorBackpressureBranch:
    """``_on_data_frame`` must recognise the
    ``backpressures_via_credit`` marker: when a recipient returns
    ``False`` *and* carries the marker, the bytes are safely buffered
    so the actor skips both ``WINDOW_UPDATE`` *and* ``RST_STREAM``.
    Recipients without the marker (regular HTTP, queue-full) still get
    ``RST_STREAM(ENHANCE_YOUR_CALM)``."""

    def _stub_stream(self, handler):
        """Minimal real Stream for ``_on_data_frame`` — beartype enforces
        the actor's annotations, so a SimpleNamespace stand-in is rejected."""
        from blackbull.protocol.stream import Stream, StreamState  # noqa: PLC0415
        stream = Stream(stream_id=1, parent=handler.root_stream)
        stream.state = StreamState.OPEN
        handler.root_stream.add_child(1)
        return stream

    async def _drive_data_frame(self, handler, stream, payload):
        """Invoke ``_on_data_frame`` directly with a synthetic DATA frame."""
        frame = handler.factory.load(_make_ws_data_frame(
            payload, stream_id=1))
        await handler._on_data_frame(frame, stream)

    async def test_backpressure_recipient_skips_window_update_and_rst(self):
        """Marker + False return → no WINDOW_UPDATE, no RST_STREAM."""
        class _BackpressureStub:
            backpressures_via_credit = True
            def put_DATAFrame(self, frame):
                return False  # full — withhold credit

        handler, _, _ = _make_h2_actor()
        # Drive the initial SETTINGS so the actor is in a steady state.
        handler.receive = AsyncMock(return_value=None)
        # Spin up just the SETTINGS path without dispatching frames.
        await handler.run()
        handler.send_frame.reset_mock()

        stream = self._stub_stream(handler)
        handler._recipients[1] = _BackpressureStub()
        await self._drive_data_frame(handler, stream, b'payload-bytes')

        kinds = [type(c.args[0]).__name__
                 for c in handler.send_frame.call_args_list]
        assert kinds == [], (
            f'backpressure path must not emit WINDOW_UPDATE or RST_STREAM; '
            f'got frames={kinds}')

    async def test_unmarked_recipient_returning_false_still_rsts(self):
        """A recipient without the marker keeps the legacy queue-full
        semantics — ``RST_STREAM(ENHANCE_YOUR_CALM)``."""
        class _DropStub:
            # No backpressures_via_credit attribute
            def put_DATAFrame(self, frame):
                return False  # queue full → drop

        handler, _, _ = _make_h2_actor()
        handler.receive = AsyncMock(return_value=None)
        await handler.run()
        handler.send_frame.reset_mock()

        stream = self._stub_stream(handler)
        handler._recipients[1] = _DropStub()
        await self._drive_data_frame(handler, stream, b'payload-bytes')

        rsts = [c.args[0] for c in handler.send_frame.call_args_list
                if hasattr(c.args[0], 'FrameType')
                and c.args[0].FrameType() == FrameTypes.RST_STREAM]
        assert len(rsts) == 1, (
            f'unmarked-recipient drop must RST_STREAM; got {rsts}')
        assert rsts[0].error_code == ErrorCodes.ENHANCE_YOUR_CALM
