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


# ---------------------------------------------------------------------------
# Helpers – minimal stream fakes
# ---------------------------------------------------------------------------

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
    """WebSocketHandler.receive() must return ASGI event dicts, not coroutines."""

    def _make_handler(self, raw_frame: bytes):
        reader = _FakeReader(raw_frame)
        writer = _FakeWriter()
        scope = {'type': 'websocket', 'path': '/ws', 'headers': []}
        return WebSocketHandler(AsyncMock(), reader, writer, scope)

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
        assert inspect.iscoroutinefunction(handler.receive)


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
        reader = _FakeReader(raw_frame)
        writer = _FakeWriter()
        scope = {'type': 'websocket', 'path': '/ws', 'headers': []}
        return WebSocketHandler(AsyncMock(), reader, writer, scope)

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
            'headers': [(b'sec-websocket-key', ws_key)],
        }

        async def fake_app(s, receive, send):
            pass

        writer = _FakeWriter()
        handler = WebSocketHandler(fake_app, _FakeReader(b''), writer, scope)
        await handler.run()

        response_text = writer.written.decode('latin-1')
        assert '101 Switching Protocols' in response_text
        assert 'Upgrade: websocket' in response_text
        assert 'Sec-WebSocket-Accept:' in response_text

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
            'headers': [(b'sec-websocket-key', ws_key)],
        }

        async def fake_app(s, receive, send):
            pass

        writer = _FakeWriter()
        handler = WebSocketHandler(fake_app, _FakeReader(b''), writer, scope)
        await handler.run()

        assert expected in writer.written.decode('latin-1')
