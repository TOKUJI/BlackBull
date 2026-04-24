"""
Tests for WebSocketHandler (blackbull/server/server.py)
========================================================

Each test is tied directly to a bug that was found during development and
explains *why* the test would have caught it early.

Bug 3 – missing ``await`` on ``self.app()``
    ``self.app(scope, self.receive, self.send)`` created a coroutine object
    but never ran it.  A test that verifies the application coroutine is
    actually *called* would have caught this immediately.

Bug 4 – ``receive()`` returned a coroutine object + wrong framing protocol
    ``return self.reader.readuntil(b'\\r\\n\\r\\n')`` returned the coroutine
    instead of awaiting it, and HTTP line-delimited reading is meaningless for
    the binary WebSocket framing protocol (RFC 6455 §5).  Unit tests for
    ``_read_frame`` with crafted byte sequences catch both issues.

Bug 5 – ``send()`` wrote raw dicts to ``writer.write()``
    ``writer.write(x)`` expects *bytes*; passing a dict raises TypeError.
    Tests that exercise every event type in ``send()`` and assert actual
    bytes appear on the wire catch this.

Bug 6 – wrong scope type passed to the application
    The handler replaced the original HTTP-upgrade scope (which contains
    ``type='websocket'`` and ``path``) with ``{'type': 'websocket.connect'}``,
    stripping the path entirely so the router could not dispatch.  A test
    that checks the scope forwarded to ``app()`` catches this.
"""

import asyncio
import struct
from unittest.mock import AsyncMock, MagicMock, call, patch
import pytest

from blackbull.server.server import WebSocketHandler
from blackbull.server.sender import WebSocketSender, AsyncioWriter
from blackbull.server.recipient import WebSocketRecipient, AsyncioReader


# ---------------------------------------------------------------------------
# Helpers – minimal stream fakes
# ---------------------------------------------------------------------------

def _make_unmasked_frame(payload: bytes, opcode: int = 0x1) -> bytes:
    """Build an *unmasked* WebSocket frame (violates RFC 6455 §5.1 for client frames)."""
    length = len(payload)
    header = bytes([0x80 | opcode])
    if length < 126:
        header += bytes([length])                          # mask bit NOT set
    elif length < 65536:
        header += bytes([126]) + length.to_bytes(2, 'big')
    else:
        header += bytes([127]) + length.to_bytes(8, 'big')
    return header + payload                                # no mask bytes, raw payload


def _make_client_frame(payload: bytes, opcode: int = 0x1) -> bytes:
    """Build a *masked* WebSocket frame (clients MUST mask, RFC 6455 §5.1)."""
    mask = b'\xde\xad\xbe\xef'
    masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    length = len(payload)
    header = bytes([0x80 | opcode])
    if length < 126:
        header += bytes([0x80 | length])
    elif length < 65536:
        header += bytes([0x80 | 126]) + length.to_bytes(2, 'big')
    else:
        header += bytes([0x80 | 127]) + length.to_bytes(8, 'big')
    return header + mask + masked


class _FakeReader:
    """Feed a pre-built byte string through asyncio StreamReader's API."""

    def __init__(self, data: bytes):
        self._buf = bytearray(data)

    async def readexactly(self, n: int) -> bytes:
        if len(self._buf) < n:
            raise asyncio.IncompleteReadError(bytes(self._buf), n)
        chunk = bytes(self._buf[:n])
        del self._buf[:n]
        return chunk

    async def readuntil(self, sep: bytes) -> bytes:
        idx = self._buf.find(sep)
        if idx == -1:
            raise asyncio.IncompleteReadError(bytes(self._buf), None)
        chunk = bytes(self._buf[:idx + len(sep)])
        del self._buf[:idx + len(sep)]
        return chunk

    async def readline(self) -> bytes:
        return await self.readuntil(b'\n')

    async def read(self, n: int) -> bytes:
        chunk = bytes(self._buf[:n])
        del self._buf[:n]
        return chunk


class _FakeWriter:
    """Capture everything written to the fake transport."""

    def __init__(self):
        self.written = bytearray()
        self.closed = False

    def write(self, data: bytes):
        self.written += data

    async def drain(self):
        pass

    def close(self):
        self.closed = True

    async def wait_closed(self):
        pass


# ---------------------------------------------------------------------------
# Compatibility wrapper — lets existing tests call handler.receive() against
# WebSocketRecipient without touching every call site.
# ---------------------------------------------------------------------------

class _RecipientWrapper:
    """Thin wrapper around WebSocketRecipient that exposes a .receive() method
    and a .writer attribute so tests written against WebSocketHandler.receive()
    continue to work unchanged."""

    def __init__(self, raw_bytes: bytes):
        self.writer = _FakeWriter()
        self._recipient = WebSocketRecipient(AsyncioReader(_FakeReader(raw_bytes)), self.writer)

    async def receive(self):
        return await self._recipient()


# ---------------------------------------------------------------------------
# _encode_frame (Bug 5 pre-condition)
# ---------------------------------------------------------------------------

class TestEncodeFrame:
    """_encode_frame must produce valid unmasked WebSocket frames."""

    def test_small_text_frame(self):
        """Payload < 126 bytes → 2-byte header."""
        payload = b'hello'
        frame = WebSocketSender._encode_frame(payload, opcode=0x1)
        assert frame[0] == 0x80 | 0x1          # FIN + text opcode
        assert frame[1] == len(payload)          # no mask bit, length 5
        assert frame[2:] == payload

    def test_medium_frame_126(self):
        """Payload == 126 bytes → 126-length indicator + 2-byte extended length."""
        payload = b'x' * 126
        frame = WebSocketSender._encode_frame(payload, opcode=0x2)
        assert frame[0] == 0x80 | 0x2
        assert frame[1] == 126
        assert int.from_bytes(frame[2:4], 'big') == 126
        assert frame[4:] == payload

    def test_large_frame(self):
        """Payload == 65536 bytes → 127-length indicator + 8-byte extended length."""
        payload = b'y' * 65536
        frame = WebSocketSender._encode_frame(payload, opcode=0x2)
        assert frame[0] == 0x80 | 0x2
        assert frame[1] == 127
        assert int.from_bytes(frame[2:10], 'big') == 65536
        assert frame[10:] == payload

    def test_binary_opcode(self):
        frame = WebSocketSender._encode_frame(b'\x00\x01', opcode=0x2)
        assert frame[0] & 0x0F == 0x2

    def test_close_frame_opcode(self):
        frame = WebSocketSender._encode_frame(b'\x03\xe8', opcode=0x8)
        assert frame[0] & 0x0F == 0x8


# ---------------------------------------------------------------------------
# _read_frame (Bug 4)
# ---------------------------------------------------------------------------

class TestReadFrame:
    """_read_frame must correctly parse masked client frames per RFC 6455."""

    @pytest.mark.asyncio
    async def test_reads_text_frame(self):
        """Bug 4: read_frame must await reader, not return a coroutine object."""
        payload = b'Toshio'
        data = _make_client_frame(payload, opcode=0x1)
        reader = _FakeReader(data)
        opcode, got = await WebSocketSender._read_frame(reader)
        assert opcode == 0x1
        assert got == payload

    @pytest.mark.asyncio
    async def test_reads_binary_frame(self):
        payload = bytes(range(16))
        data = _make_client_frame(payload, opcode=0x2)
        reader = _FakeReader(data)
        opcode, got = await WebSocketSender._read_frame(reader)
        assert opcode == 0x2
        assert got == payload

    @pytest.mark.asyncio
    async def test_reads_close_frame(self):
        payload = b'\x03\xe8'
        data = _make_client_frame(payload, opcode=0x8)
        reader = _FakeReader(data)
        opcode, got = await WebSocketSender._read_frame(reader)
        assert opcode == 0x8
        assert got == payload

    @pytest.mark.asyncio
    async def test_unmask_is_applied(self):
        """Masked frame: payload must be XOR-unmasked correctly."""
        raw_payload = b'ABCD'
        data = _make_client_frame(raw_payload, opcode=0x1)
        reader = _FakeReader(data)
        _, got = await WebSocketSender._read_frame(reader)
        assert got == raw_payload

    @pytest.mark.asyncio
    async def test_medium_payload_extended_length(self):
        """Frames with 126-byte payloads use the 16-bit extended-length field."""
        payload = b'z' * 126
        data = _make_client_frame(payload, opcode=0x1)
        reader = _FakeReader(data)
        opcode, got = await WebSocketSender._read_frame(reader)
        assert got == payload

    @pytest.mark.asyncio
    async def test_incomplete_read_raises(self):
        """Truncated stream must raise IncompleteReadError, not return partial data."""
        reader = _FakeReader(b'\x81')   # only 1 byte – header requires 2
        with pytest.raises(asyncio.IncompleteReadError):
            await WebSocketSender._read_frame(reader)


# ---------------------------------------------------------------------------
# receive() (Bug 4)
# ---------------------------------------------------------------------------

class TestReceive:
    """WebSocketRecipient must return ASGI event dicts, not coroutines."""

    def _make_handler(self, raw_frame: bytes):
        return _RecipientWrapper(raw_frame)

    @pytest.mark.asyncio
    async def test_receive_text_returns_dict(self):
        """Bug 4 regression: receive() must return an awaited dict, not a coroutine.

        The first call returns websocket.connect (ASGI spec); the second call
        reads the actual frame from the wire.
        """
        handler = self._make_handler(_make_client_frame(b'hello', opcode=0x1))
        await handler.receive()      # websocket.connect — skip
        event = await handler.receive()
        # Must be a plain dict – never a coroutine
        assert isinstance(event, dict), "receive() returned a coroutine instead of a dict"
        assert event['type'] == 'websocket.receive'
        assert event['text'] == 'hello'
        assert event['bytes'] is None

    @pytest.mark.asyncio
    async def test_receive_binary_returns_dict(self):
        handler = self._make_handler(_make_client_frame(b'\xde\xad', opcode=0x2))
        await handler.receive()      # websocket.connect — skip
        event = await handler.receive()
        assert event['type'] == 'websocket.receive'
        assert event['text'] is None
        assert event['bytes'] == b'\xde\xad'

    @pytest.mark.asyncio
    async def test_receive_close_returns_disconnect(self):
        handler = self._make_handler(_make_client_frame(b'\x03\xe8', opcode=0x8))
        await handler.receive()      # websocket.connect — skip
        event = await handler.receive()
        assert event['type'] == 'websocket.disconnect'

    @pytest.mark.asyncio
    async def test_receive_is_awaitable(self):
        """Verify receive() is a coroutine function so callers can await it."""
        handler = self._make_handler(_make_client_frame(b'x', opcode=0x1))
        import inspect
        assert inspect.iscoroutinefunction(handler.receive) or inspect.iscoroutinefunction(handler._recipient.__call__)


# ---------------------------------------------------------------------------
# receive() — websocket.connect on first call (P1 spec requirement)
# ---------------------------------------------------------------------------
#
# ASGI WebSocket spec §4.2:
#   The first event sent to the application on a WebSocket connection MUST be
#   ``{"type": "websocket.connect"}``.  Only *after* that may the handler
#   read actual WebSocket frames from the wire.
#
# The current implementation skips this and goes straight to frame reading,
# which breaks any ASGI middleware that guards on the connect event (e.g.
# ``middlewares.websocket()`` which raises ValueError when the first message
# is not ``websocket.connect``).

class TestReceiveConnect:
    """receive() must return websocket.connect on the first call."""

    def _make_handler(self, raw_frame: bytes = b''):
        return _RecipientWrapper(raw_frame)

    @pytest.mark.asyncio
    async def test_first_call_returns_connect(self):
        """First receive() must return websocket.connect without reading a frame."""
        # The reader has a valid text frame, but the first call must NOT consume it.
        handler = self._make_handler(_make_client_frame(b'hello', opcode=0x1))
        event = await handler.receive()
        assert event == {'type': 'websocket.connect'}, (
            f"First receive() must return websocket.connect, got {event!r}"
        )

    @pytest.mark.asyncio
    async def test_first_call_does_not_read_from_wire(self):
        """First receive() must not consume any bytes from the reader."""
        # Supply an empty reader — if the handler tries to read, it will raise
        # IncompleteReadError, causing the test to fail.
        handler = self._make_handler(b'')
        event = await handler.receive()
        assert event['type'] == 'websocket.connect'

    @pytest.mark.asyncio
    async def test_second_call_reads_first_frame(self):
        """Second receive() must read the first actual WebSocket frame."""
        handler = self._make_handler(_make_client_frame(b'hello', opcode=0x1))
        await handler.receive()          # consume the connect event
        event = await handler.receive()  # now reads the wire frame
        assert event['type'] == 'websocket.receive'
        assert event['text'] == 'hello'

    @pytest.mark.asyncio
    async def test_connect_emitted_only_once(self):
        """websocket.connect must appear exactly once across multiple receive() calls."""
        frames = (
            _make_client_frame(b'first', opcode=0x1)
            + _make_client_frame(b'second', opcode=0x1)
        )
        handler = self._make_handler(frames)

        events = [await handler.receive() for _ in range(3)]
        connect_events = [e for e in events if e.get('type') == 'websocket.connect']
        assert len(connect_events) == 1, (
            f"websocket.connect appeared {len(connect_events)} times; expected exactly 1"
        )

    @pytest.mark.asyncio
    async def test_third_call_reads_second_frame(self):
        """Calls after the connect event continue to read frames in order."""
        frames = (
            _make_client_frame(b'alpha', opcode=0x1)
            + _make_client_frame(b'beta', opcode=0x1)
        )
        handler = self._make_handler(frames)
        await handler.receive()              # websocket.connect
        first = await handler.receive()      # reads 'alpha'
        second = await handler.receive()     # reads 'beta'
        assert first['text'] == 'alpha'
        assert second['text'] == 'beta'

    @pytest.mark.asyncio
    async def test_connect_then_close_frame_returns_disconnect(self):
        """After the connect event, a close frame must return websocket.disconnect."""
        handler = self._make_handler(_make_client_frame(b'\x03\xe8', opcode=0x8))
        connect = await handler.receive()
        assert connect['type'] == 'websocket.connect'
        disconnect = await handler.receive()
        assert disconnect['type'] == 'websocket.disconnect'


# ---------------------------------------------------------------------------
# Ping / Pong (P1 item 4)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestPingPong:
    """WebSocketHandler.receive() must reply to ping frames with a pong frame
    (RFC 6455 §5.5.1) and silently drop unsolicited pong frames (§5.5.3),
    in both cases without surfacing the control frame to the ASGI application.
    """

    def _make_handler(self, raw_bytes: bytes):
        return _RecipientWrapper(raw_bytes)

    async def test_ping_causes_pong_written_to_wire(self):
        """Ping frame must cause a pong frame to appear on the wire."""
        ping_payload = b'ping-data'
        frames = (_make_client_frame(ping_payload, opcode=0x9)
                  + _make_client_frame(b'hello', opcode=0x1))
        handler = self._make_handler(frames)
        await handler.receive()       # consume connect event
        await handler.receive()       # process ping, return next data frame
        expected_pong = WebSocketSender._encode_frame(ping_payload, opcode=0xA)
        assert handler.writer.written == expected_pong

    async def test_pong_payload_matches_ping_payload(self):
        """Pong payload must be identical to ping payload (RFC 6455 §5.5.2)."""
        ping_payload = b'\x01\x02\x03\x04'
        frames = (_make_client_frame(ping_payload, opcode=0x9)
                  + _make_client_frame(b'', opcode=0x8))  # close to end loop
        handler = self._make_handler(frames)
        await handler.receive()       # connect
        await handler.receive()       # ping → pong, then close → disconnect
        pong = WebSocketSender._encode_frame(ping_payload, opcode=0xA)
        assert handler.writer.written == pong

    async def test_empty_ping_causes_empty_pong(self):
        """Ping with empty payload must produce a pong with empty payload."""
        frames = (_make_client_frame(b'', opcode=0x9)
                  + _make_client_frame(b'hi', opcode=0x1))
        handler = self._make_handler(frames)
        await handler.receive()       # connect
        await handler.receive()       # empty ping → empty pong, then text
        expected_pong = WebSocketSender._encode_frame(b'', opcode=0xA)
        assert handler.writer.written == expected_pong

    async def test_ping_is_transparent_to_app(self):
        """receive() must return the data frame that follows the ping, not the ping itself."""
        frames = (_make_client_frame(b'ignored', opcode=0x9)
                  + _make_client_frame(b'hello', opcode=0x1))
        handler = self._make_handler(frames)
        await handler.receive()       # connect
        event = await handler.receive()
        assert event == {'type': 'websocket.receive', 'text': 'hello', 'bytes': None}

    async def test_consecutive_pings_all_replied(self):
        """Two consecutive ping frames must each produce a pong in order."""
        p1 = b'first'
        p2 = b'second'
        frames = (_make_client_frame(p1, opcode=0x9)
                  + _make_client_frame(p2, opcode=0x9)
                  + _make_client_frame(b'end', opcode=0x1))
        handler = self._make_handler(frames)
        await handler.receive()       # connect
        await handler.receive()       # two pings handled, text returned
        pong1 = WebSocketSender._encode_frame(p1, opcode=0xA)
        pong2 = WebSocketSender._encode_frame(p2, opcode=0xA)
        assert handler.writer.written == pong1 + pong2

    async def test_unsolicited_pong_is_silently_dropped(self):
        """Unsolicited pong frame (0xA) must be silently ignored — nothing written."""
        frames = (_make_client_frame(b'data', opcode=0xA)
                  + _make_client_frame(b'hello', opcode=0x1))
        handler = self._make_handler(frames)
        await handler.receive()       # connect
        event = await handler.receive()
        assert event == {'type': 'websocket.receive', 'text': 'hello', 'bytes': None}
        assert handler.writer.written == b''


# ---------------------------------------------------------------------------
# Unmasked client frames (P1 item 5)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestUnmaskedFrames:
    """WebSocketHandler.receive() must reject unmasked client frames.

    RFC 6455 §5.1:
        A server MUST close the connection upon receiving a frame that is not
        masked.  In this case, a server MAY send a Close frame with a status
        code of 1002 (protocol error) as defined in Section 7.4.1.

    Clients MUST mask every frame they send to the server (mask bit = 1 in the
    second byte of the frame header).  Receiving an unmasked frame is a
    protocol violation that must not be silently ignored.
    """

    def _make_handler(self, raw_bytes: bytes):
        return _RecipientWrapper(raw_bytes)

    async def test_unmasked_text_frame_raises(self):
        """Unmasked text frame (opcode 0x1) must be rejected."""
        handler = self._make_handler(_make_unmasked_frame(b'hello', opcode=0x1))
        await handler.receive()       # consume websocket.connect
        with pytest.raises(Exception):
            await handler.receive()

    async def test_unmasked_binary_frame_raises(self):
        """Unmasked binary frame (opcode 0x2) must be rejected."""
        handler = self._make_handler(_make_unmasked_frame(b'\x00\x01', opcode=0x2))
        await handler.receive()
        with pytest.raises(Exception):
            await handler.receive()

    async def test_unmasked_ping_frame_raises(self):
        """Unmasked ping frame (opcode 0x9) must be rejected before replying."""
        handler = self._make_handler(_make_unmasked_frame(b'', opcode=0x9))
        await handler.receive()
        with pytest.raises(Exception):
            await handler.receive()

    async def test_unmasked_close_frame_raises(self):
        """Unmasked close frame (opcode 0x8) must be rejected."""
        handler = self._make_handler(_make_unmasked_frame(b'\x03\xe8', opcode=0x8))
        await handler.receive()
        with pytest.raises(Exception):
            await handler.receive()

    async def test_unmasked_empty_payload_raises(self):
        """Unmasked frame with empty payload must still be rejected."""
        handler = self._make_handler(_make_unmasked_frame(b'', opcode=0x1))
        await handler.receive()
        with pytest.raises(Exception):
            await handler.receive()

    async def test_masked_frame_still_accepted(self):
        """Properly masked frame must continue to be accepted (regression)."""
        handler = self._make_handler(_make_client_frame(b'hello', opcode=0x1))
        await handler.receive()       # connect
        event = await handler.receive()
        assert event == {'type': 'websocket.receive', 'text': 'hello', 'bytes': None}


# ---------------------------------------------------------------------------
# send() (Bug 5)
# ---------------------------------------------------------------------------

class TestSend:
    """WebSocketSender must encode ASGI event dicts into wire bytes."""

    def _make_sender(self):
        writer = _FakeWriter()
        sender = WebSocketSender(AsyncioWriter(writer))
        return sender, writer

    @pytest.mark.asyncio
    async def test_send_text_writes_bytes(self):
        """Bug 5 regression: send() must write bytes to the wire, not a raw dict."""
        sender, writer = self._make_sender()
        await sender({'type': 'websocket.send', 'text': 'hello'})
        assert len(writer.written) > 0, "send() wrote nothing to the wire"
        assert isinstance(writer.written, (bytes, bytearray))

    @pytest.mark.asyncio
    async def test_send_text_produces_text_opcode(self):
        """Text payload must use opcode 0x1 (RFC 6455 §5.2)."""
        sender, writer = self._make_sender()
        await sender({'type': 'websocket.send', 'text': 'hi'})
        assert writer.written[0] & 0x0F == 0x1

    @pytest.mark.asyncio
    async def test_send_text_payload_is_utf8(self):
        sender, writer = self._make_sender()
        msg = 'Toshio'
        await sender({'type': 'websocket.send', 'text': msg})
        # Parse back: 2-byte header (FIN+opcode, length), then payload
        length = writer.written[1] & 0x7F
        payload = bytes(writer.written[2:2 + length])
        assert payload == msg.encode('utf-8')

    @pytest.mark.asyncio
    async def test_send_bytes_writes_binary_opcode(self):
        """Binary payload must use opcode 0x2."""
        sender, writer = self._make_sender()
        await sender({'type': 'websocket.send', 'bytes': b'\xca\xfe'})
        assert writer.written[0] & 0x0F == 0x2

    @pytest.mark.asyncio
    async def test_send_close_writes_close_opcode(self):
        """websocket.close event must produce a close frame (opcode 0x8)."""
        sender, writer = self._make_sender()
        await sender({'type': 'websocket.close'})
        assert len(writer.written) > 0
        assert writer.written[0] & 0x0F == 0x8

    @pytest.mark.asyncio
    async def test_send_accept_writes_nothing(self):
        """websocket.accept is handled in run(); send() should be a no-op for it."""
        sender, writer = self._make_sender()
        await sender({'type': 'websocket.accept', 'subprotocol': None})
        assert len(writer.written) == 0


# ---------------------------------------------------------------------------
# run() scope forwarding (Bug 6)
# ---------------------------------------------------------------------------

class TestRunScopeForwarding:
    """WebSocketHandler.run() must forward the original HTTP-upgrade scope to app.

    Bug 6: the old code replaced the scope with ``{'type': 'websocket.connect'}``
    (no 'path' key), which broke router dispatch.
    """

    def _make_upgrade_scope(self, path: str = '/ws') -> dict:
        """Minimal scope as produced by parse() from an HTTP Upgrade request."""
        # The Sec-WebSocket-Key value doesn't have to be valid Base64 here
        # because we're only testing scope forwarding, not the handshake itself.
        ws_key = b'dGhlIHNhbXBsZSBub25jZQ=='
        return {
            'type': 'websocket',
            'path': path,
            'scheme': 'ws',
            'headers': [
                (b'Host', b'localhost:9999'),
                (b'Upgrade', b'websocket'),
                (b'sec-websocket-key', ws_key),
                (b'sec-websocket-version', b'13'),
            ],
        }

    @pytest.mark.asyncio
    async def test_app_is_awaited(self):
        """Bug 3 regression: app() must be awaited, not just called."""
        scope = self._make_upgrade_scope()

        called_with = {}

        async def fake_app(s, receive, send):
            called_with['scope'] = s

        reader = _FakeReader(b'')   # nothing to read after handshake
        writer = _FakeWriter()
        handler = WebSocketHandler(fake_app, reader, writer, scope)

        await handler.run()

        assert called_with, "app coroutine was never awaited (Bug 3)"

    @pytest.mark.asyncio
    async def test_app_receives_original_scope_type(self):
        """Bug 6 regression: scope type forwarded to app must be 'websocket'."""
        scope = self._make_upgrade_scope('/chat')
        captured = {}

        async def fake_app(s, receive, send):
            captured['scope'] = s

        reader = _FakeReader(b'')
        writer = _FakeWriter()
        handler = WebSocketHandler(fake_app, reader, writer, scope)
        await handler.run()

        fwd = captured['scope']
        assert fwd['type'] == 'websocket', (
            f"Expected type='websocket', got type={fwd['type']!r}. "
            "The handler must NOT replace the scope with {{'type': 'websocket.connect'}}."
        )

    @pytest.mark.asyncio
    async def test_app_receives_original_path(self):
        """Bug 6 regression: 'path' must survive forwarding to the application."""
        scope = self._make_upgrade_scope('/chat')
        captured = {}

        async def fake_app(s, receive, send):
            captured['scope'] = s

        reader = _FakeReader(b'')
        writer = _FakeWriter()
        handler = WebSocketHandler(fake_app, reader, writer, scope)
        await handler.run()

        assert 'path' in captured['scope'], (
            "scope forwarded to app is missing 'path' – router cannot dispatch."
        )
        assert captured['scope']['path'] == '/chat'

    @pytest.mark.asyncio
    async def test_app_not_called_with_websocket_connect_type(self):
        """The old incorrect scope {'type': 'websocket.connect'} must never appear."""
        scope = self._make_upgrade_scope()
        captured = {}

        async def fake_app(s, receive, send):
            captured['scope'] = s

        reader = _FakeReader(b'')
        writer = _FakeWriter()
        handler = WebSocketHandler(fake_app, reader, writer, scope)
        await handler.run()

        assert captured['scope'].get('type') != 'websocket.connect', (
            "app received the old incorrect scope type 'websocket.connect'"
        )


# ---------------------------------------------------------------------------
# run() handshake response (general correctness)
# ---------------------------------------------------------------------------

class TestRunHandshake:
    """run() must write a valid HTTP/1.1 101 Switching Protocols response."""

    @pytest.mark.asyncio
    async def test_101_header_is_written(self):
        ws_key = b'dGhlIHNhbXBsZSBub25jZQ=='
        scope = {
            'type': 'websocket',
            'path': '/ws',
            'scheme': 'ws',
            'headers': [(b'sec-websocket-key', ws_key), (b'sec-websocket-version', b'13')],
        }

        async def fake_app(s, receive, send):
            pass

        writer = _FakeWriter()
        handler = WebSocketHandler(fake_app, _FakeReader(b''), writer, scope)
        await handler.run()

        response_text = writer.written.decode('latin-1').lower()
        assert '101 Switching Protocols'.lower() in response_text
        assert 'Upgrade: websocket'.lower() in response_text
        assert 'Sec-WebSocket-Accept:'.lower() in response_text

    @pytest.mark.asyncio
    async def test_accept_key_is_correct(self):
        """The Sec-WebSocket-Accept value must follow RFC 6455 §4.2.2."""
        import base64, hashlib
        ws_key = b'dGhlIHNhbXBsZSBub25jZQ=='
        magic = b'258EAFA5-E914-47DA-95CA-C5AB0DC85B11'
        expected = base64.b64encode(
            hashlib.sha1(ws_key + magic).digest()
        ).decode('ascii')

        scope = {
            'type': 'websocket', 'path': '/ws', 'scheme': 'ws',
            'headers': [(b'sec-websocket-key', ws_key), (b'sec-websocket-version', b'13')],
        }

        async def fake_app(s, receive, send):
            pass

        writer = _FakeWriter()
        handler = WebSocketHandler(fake_app, _FakeReader(b''), writer, scope)
        await handler.run()

        assert expected in writer.written.decode('latin-1')


# ---------------------------------------------------------------------------
# P3 — WebSocket Sec-WebSocket-Version: 13 validation
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestWebSocketVersionValidation:
    """WebSocketHandler.run() must validate Sec-WebSocket-Version before
    completing the handshake (RFC 6455 §4.2.1, §4.4).

    The server MUST check that the version is '13'.  Any other value (or a
    missing header) must result in a 400 Bad Request response that includes
    Sec-WebSocket-Version: 13 so the client knows which version to retry with.
    """

    def _make_handler(self, version: bytes | None = b'13'):
        headers = [(b'sec-websocket-key', b'dGhlIHNhbXBsZSBub25jZQ==')]
        if version is not None:
            headers.append((b'sec-websocket-version', version))
        scope = {'type': 'websocket', 'path': '/ws', 'headers': headers}
        writer = _FakeWriter()

        async def app(scope, receive, send):
            pass

        handler = WebSocketHandler(app, _FakeReader(b''), writer, scope)
        return handler, writer

    async def test_version_13_completes_101_handshake(self):
        """Version 13 must result in a 101 Switching Protocols response with no error."""
        handler, writer = self._make_handler(version=b'13')
        await handler.run()
        wire = bytes(writer.written)
        assert b'101' in wire, (
            f'Expected 101 for Sec-WebSocket-Version: 13; got {wire!r}'
        )
        assert b'400' not in wire, (
            f'Valid version 13 must not produce a 400 error; got {wire!r}'
        )

    async def test_version_13_calls_app(self):
        """Version 13 must call the ASGI app (RFC 6455 §4.2.2)."""
        called = []

        async def app(scope, receive, send):
            called.append(True)

        headers = [(b'sec-websocket-key', b'dGhlIHNhbXBsZSBub25jZQ=='),
                   (b'sec-websocket-version', b'13')]
        scope = {'type': 'websocket', 'path': '/ws', 'headers': headers}
        handler = WebSocketHandler(app, _FakeReader(b''), _FakeWriter(), scope)
        await handler.run()
        assert called, 'ASGI app must be called for a valid WebSocket handshake'

    async def test_missing_version_returns_400(self):
        """Missing Sec-WebSocket-Version must produce HTTP 400 and abort (RFC 6455 §4.4)."""
        handler, writer = self._make_handler(version=None)
        await handler.run()
        wire = bytes(writer.written)
        assert b'400' in wire, (
            f'Expected 400 for missing version header; got {wire!r}'
        )
        assert b'101' not in wire, (
            f'Missing version must abort the handshake — 101 must not be sent; got {wire!r}'
        )

    async def test_missing_version_does_not_call_app(self):
        """Missing Sec-WebSocket-Version must not invoke the ASGI app (RFC 6455 §4.2.2)."""
        called = []

        async def app(scope, receive, send):
            called.append(True)

        scope = {'type': 'websocket', 'path': '/ws',
                 'headers': [(b'sec-websocket-key', b'dGhlIHNhbXBsZSBub25jZQ==')]}
        handler = WebSocketHandler(app, _FakeReader(b''), _FakeWriter(), scope)
        await handler.run()
        assert not called, 'ASGI app must NOT be called when version header is missing'

    async def test_wrong_version_returns_400(self):
        """Sec-WebSocket-Version: 8 (or any value != 13) must produce HTTP 400 and abort."""
        handler, writer = self._make_handler(version=b'8')
        await handler.run()
        wire = bytes(writer.written)
        assert b'400' in wire, (
            f'Expected 400 for version 8; got {wire!r}'
        )
        assert b'101' not in wire, (
            f'Wrong version must abort the handshake — 101 must not be sent; got {wire!r}'
        )

    async def test_wrong_version_does_not_call_app(self):
        """Wrong Sec-WebSocket-Version must not invoke the ASGI app (RFC 6455 §4.2.2)."""
        called = []

        async def app(scope, receive, send):
            called.append(True)

        headers = [(b'sec-websocket-key', b'dGhlIHNhbXBsZSBub25jZQ=='),
                   (b'sec-websocket-version', b'8')]
        scope = {'type': 'websocket', 'path': '/ws', 'headers': headers}
        handler = WebSocketHandler(app, _FakeReader(b''), _FakeWriter(), scope)
        await handler.run()
        assert not called, 'ASGI app must NOT be called when version is not 13'

    async def test_wrong_version_response_advertises_version_13(self):
        """400 response must include Sec-WebSocket-Version: 13 (RFC 6455 §4.4)."""
        handler, writer = self._make_handler(version=b'8')
        await handler.run()
        wire = bytes(writer.written).lower()
        assert b'sec-websocket-version: 13' in wire, (
            f'400 response must advertise version 13; got {wire!r}'
        )


# ---------------------------------------------------------------------------
# P3 — WebSocket subprotocol negotiation
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestWebSocketSubprotocol:
    """WebSocketHandler must negotiate Sec-WebSocket-Protocol automatically.

    The server picks the first client-offered protocol that appears in the
    app's ``available_ws_protocols`` registry and includes it in the 101
    response (RFC 6455 §4.2.2, §11.3.4).  The app handler does not need to
    inspect or echo the protocol — negotiation is done before the app is called.
    """

    def _make_handler(self, client_protocols: bytes | None,
                      available: list[bytes]):
        headers = [
            (b'sec-websocket-key', b'dGhlIHNhbXBsZSBub25jZQ=='),
            (b'sec-websocket-version', b'13'),
        ]
        if client_protocols is not None:
            headers.append((b'sec-websocket-protocol', client_protocols))
        scope = {'type': 'websocket', 'path': '/ws', 'headers': headers}
        writer = _FakeWriter()

        async def app(*_):
            pass
        app.available_ws_protocols = available

        handler = WebSocketHandler(app, _FakeReader(b''), writer, scope)
        return handler, writer

    async def test_matched_protocol_in_101_response(self):
        """Server registry contains 'chat': 101 must include Sec-WebSocket-Protocol."""
        handler, writer = self._make_handler(b'chat, superchat',
                                             available=[b'chat', b'superchat'])
        await handler.run()
        wire = bytes(writer.written).lower()
        assert b'sec-websocket-protocol' in wire, (
            f'Expected Sec-WebSocket-Protocol in 101 response; got {wire!r}'
        )

    async def test_matched_protocol_value_is_first_client_match(self):
        """Server picks the first client-offered protocol present in its registry."""
        handler, writer = self._make_handler(b'chat, superchat',
                                             available=[b'chat', b'superchat'])
        await handler.run()
        wire = bytes(writer.written)
        assert b'chat' in wire, (
            f'Expected "chat" in handshake response; got {wire!r}'
        )

    async def test_no_match_omits_subprotocol_header(self):
        """When no client protocol is in the registry, 101 must omit the header."""
        handler, writer = self._make_handler(b'graphql-ws',
                                             available=[b'chat'])
        await handler.run()
        wire = bytes(writer.written).lower()
        assert b'sec-websocket-protocol' not in wire, (
            f'Unexpected Sec-WebSocket-Protocol when no match; got {wire!r}'
        )

    async def test_empty_registry_omits_subprotocol_header(self):
        """App with no registered protocols must not produce Sec-WebSocket-Protocol."""
        handler, writer = self._make_handler(b'chat', available=[])
        await handler.run()
        wire = bytes(writer.written).lower()
        assert b'sec-websocket-protocol' not in wire, (
            f'Unexpected Sec-WebSocket-Protocol with empty registry; got {wire!r}'
        )

    async def test_no_client_request_omits_subprotocol_header(self):
        """Without a client Sec-WebSocket-Protocol header, none must appear in 101."""
        handler, writer = self._make_handler(client_protocols=None,
                                             available=[b'chat'])
        await handler.run()
        wire = bytes(writer.written).lower()
        assert b'sec-websocket-protocol' not in wire, (
            f'Unexpected Sec-WebSocket-Protocol without client request; got {wire!r}'
        )

    async def test_no_available_ws_protocols_attr_omits_header(self):
        """Generic ASGI app without available_ws_protocols must not crash or add header."""
        headers = [
            (b'sec-websocket-key', b'dGhlIHNhbXBsZSBub25jZQ=='),
            (b'sec-websocket-version', b'13'),
            (b'sec-websocket-protocol', b'chat'),
        ]
        scope = {'type': 'websocket', 'path': '/ws', 'headers': headers}
        writer = _FakeWriter()

        async def plain_asgi_app(*_):
            pass  # no available_ws_protocols attribute

        handler = WebSocketHandler(plain_asgi_app, _FakeReader(b''), writer, scope)
        await handler.run()
        wire = bytes(writer.written).lower()
        assert b'sec-websocket-protocol' not in wire, (
            f'Generic ASGI app must not produce Sec-WebSocket-Protocol; got {wire!r}'
        )


# ---------------------------------------------------------------------------
# P3 — WebSocket fragmentation
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestWebSocketFragmentation:
    """WebSocketRecipient must reassemble fragmented messages (RFC 6455 §5.4).

    A fragmented message consists of:
      - An initial frame with FIN=0 and opcode = TEXT or BINARY
      - Zero or more continuation frames with FIN=0 and opcode=0 (CONTINUATION)
      - A final continuation frame with FIN=1 and opcode=0

    The recipient must deliver a single 'websocket.receive' event with the
    concatenated payload — the app must never see raw fragments.
    """

    @staticmethod
    def _nonfin_text_frame(payload: bytes) -> bytes:
        """Masked text frame with FIN=0 (start of a fragmented message)."""
        mask = b'\xde\xad\xbe\xef'
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        header = bytes([0x01, 0x80 | len(payload)])  # FIN=0 opcode=1, mask bit
        return header + mask + masked

    @staticmethod
    def _continuation_frame(payload: bytes, fin: bool = True) -> bytes:
        """Masked continuation frame (opcode=0x0), FIN controlled by caller."""
        mask = b'\xde\xad\xbe\xef'
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        fin_bit = 0x80 if fin else 0x00
        header = bytes([fin_bit | 0x00, 0x80 | len(payload)])  # opcode=0, mask bit
        return header + mask + masked

    async def test_two_fragments_reassembled_into_one_event(self):
        """FIN=0 text frame + FIN=1 continuation must yield one websocket.receive."""
        frames = self._nonfin_text_frame(b'hel') + self._continuation_frame(b'lo')
        handler = _RecipientWrapper(frames)
        await handler.receive()  # websocket.connect
        event = await handler.receive()

        assert event['type'] == 'websocket.receive', event
        assert event.get('text') == 'hello', (
            f"Expected text='hello' from reassembled fragments; got {event!r}"
        )

    async def test_three_fragments_reassembled(self):
        """Three consecutive fragments must merge into a single event."""
        frames = (self._nonfin_text_frame(b'foo')
                  + self._continuation_frame(b'bar', fin=False)
                  + self._continuation_frame(b'baz', fin=True))
        handler = _RecipientWrapper(frames)
        await handler.receive()  # websocket.connect
        event = await handler.receive()

        assert event.get('text') == 'foobarbaz', (
            f"Expected text='foobarbaz' from 3 fragments; got {event!r}"
        )

    async def test_unfragmented_frame_still_works(self):
        """A normal FIN=1 frame must not be broken by fragmentation support."""
        handler = _RecipientWrapper(_make_client_frame(b'hello', opcode=0x1))
        await handler.receive()  # websocket.connect
        event = await handler.receive()
        assert event == {'type': 'websocket.receive', 'text': 'hello', 'bytes': None}

    async def test_fragmented_message_followed_by_normal_message(self):
        """After a reassembled fragmented message, subsequent frames must work."""
        frames = (self._nonfin_text_frame(b'frag')
                  + self._continuation_frame(b'ment', fin=True)
                  + _make_client_frame(b'normal', opcode=0x1))
        handler = _RecipientWrapper(frames)
        await handler.receive()  # websocket.connect
        first = await handler.receive()
        second = await handler.receive()

        assert first.get('text') == 'fragment', (
            f"Expected first='fragment'; got {first!r}"
        )
        assert second.get('text') == 'normal', (
            f"Expected second='normal'; got {second!r}"
        )

    # --- Error cases (RFC 6455 §5.4, §5.5) ---

    @staticmethod
    def _nonfin_control_frame(payload: bytes, opcode: int) -> bytes:
        """Masked control frame with FIN=0 — a protocol violation per RFC 6455 §5.5."""
        mask = b'\xde\xad\xbe\xef'
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        # byte 0: FIN=0, opcode; byte 1: MASK=1, length
        header = bytes([opcode, 0x80 | len(payload)])
        return header + mask + masked

    async def test_orphan_continuation_raises(self):
        """Continuation frame with no fragmentation in progress must raise.

        RFC 6455 §5.4: a CONTINUATION frame is only valid after a FIN=0
        data frame.  Receiving one without a preceding opener is a protocol
        error that the server must not silently swallow.
        """
        handler = _RecipientWrapper(self._continuation_frame(b'orphan', fin=True))
        await handler.receive()  # websocket.connect
        with pytest.raises(Exception):
            await handler.receive()

    async def test_new_data_frame_during_fragmentation_raises(self):
        """Starting a new data frame while a fragmented message is open must raise.

        RFC 6455 §5.4: once a FIN=0 opener has been received, the sender
        MUST send only CONTINUATION frames (or control frames) until the
        final FIN=1 continuation closes the message.  A new TEXT or BINARY
        opener mid-sequence is a protocol error.
        """
        frames = (self._nonfin_text_frame(b'start')
                  + _make_client_frame(b'interloper', opcode=0x1))
        handler = _RecipientWrapper(frames)
        await handler.receive()  # websocket.connect
        with pytest.raises(Exception):
            await handler.receive()

    async def test_fragmented_control_frame_raises(self):
        """A control frame with FIN=0 must be rejected.

        RFC 6455 §5.5: "All control frames MUST have a payload length of 125
        bytes or less and MUST NOT be fragmented."  A ping/pong/close frame
        with FIN=0 is a protocol error regardless of whether a data
        fragmentation is in progress.
        """
        handler = _RecipientWrapper(self._nonfin_control_frame(b'', opcode=0x9))
        await handler.receive()  # websocket.connect
        with pytest.raises(Exception):
            await handler.receive()

    async def test_ping_interleaved_during_fragmentation(self):
        """A ping received between data fragments must be replied to immediately
        and the fragmented message must still be reassembled correctly.

        RFC 6455 §5.5: control frames MAY be injected between fragments of a
        data message.  The server must handle them transparently — the pong
        goes out and the app sees a single reassembled websocket.receive event.
        """
        ping_payload = b'keepalive'
        frames = (
            self._nonfin_text_frame(b'hel')
            + _make_client_frame(ping_payload, opcode=0x9)  # FIN=1 ping (legal)
            + self._continuation_frame(b'lo', fin=True)
        )
        handler = _RecipientWrapper(frames)
        await handler.receive()  # websocket.connect
        event = await handler.receive()

        assert event.get('text') == 'hello', (
            f"Expected reassembled text='hello' after interleaved ping; got {event!r}"
        )
        expected_pong = WebSocketSender._encode_frame(ping_payload, opcode=0xA)
        assert handler.writer.written == expected_pong, (
            f"Expected pong on wire after interleaved ping; got {handler.writer.written!r}"
        )


# ---------------------------------------------------------------------------
# P3 — WebSocket per-message deflate (RFC 7692)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestWebSocketDeflate:
    """WebSocketRecipient must decompress per-message deflate payloads.

    The permessage-deflate extension (RFC 7692 §7) sets RSV1=1 in the first
    frame of a compressed message.  The payload uses raw DEFLATE (no zlib
    header/trailer) and is terminated by the synthetic tail bytes 0x00 0x00
    0xFF 0xFF before stripping.

    The extension must be negotiated in the WebSocket handshake, but
    recipient-level tests inject frames directly to keep tests focused.
    """

    @staticmethod
    def _deflate_frame(payload: bytes) -> bytes:
        """Masked text frame with RSV1=1 and DEFLATE-compressed payload."""
        import zlib
        compressed = zlib.compress(payload, level=6)
        # Strip the 2-byte zlib header and 4-byte checksum; add RFC 7692 tail
        raw_deflate = compressed[2:-4]
        mask = b'\xde\xad\xbe\xef'
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(raw_deflate))
        length = len(raw_deflate)
        # FIN=1 RSV1=1 opcode=0x1: byte 0 = 0b11000001 = 0xC1
        header = bytes([0xC1])
        if length < 126:
            header += bytes([0x80 | length])
        elif length < 65536:
            header += bytes([0x80 | 126]) + length.to_bytes(2, 'big')
        else:
            header += bytes([0x80 | 127]) + length.to_bytes(8, 'big')
        return header + mask + masked

    async def test_deflate_frame_decompressed(self):
        """RSV1=1 text frame must be decompressed before the app sees it."""
        payload = b'Hello, compressed world!'
        handler = _RecipientWrapper(self._deflate_frame(payload))
        await handler.receive()  # websocket.connect
        event = await handler.receive()

        assert event['type'] == 'websocket.receive'
        assert event.get('text') == payload.decode(), (
            f"Expected decompressed text={payload!r}; got {event!r}"
        )

    async def test_uncompressed_frame_unaffected_by_deflate_support(self):
        """A normal (RSV1=0) frame must pass through unchanged."""
        handler = _RecipientWrapper(_make_client_frame(b'plain', opcode=0x1))
        await handler.receive()  # websocket.connect
        event = await handler.receive()
        assert event.get('text') == 'plain', (
            f"Uncompressed frame must not be decompressed; got {event!r}"
        )
