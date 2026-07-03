"""Unit tests for WebSocket frame-level behavior (WebSocketRecipient / WebSocketSender).

Each class protects a specific RFC 6455 contract at the component boundary.
Tests drive WebSocketRecipient and WebSocketSender directly with in-process
byte buffers — no sockets, no actor loop.
"""
import asyncio
import struct
from unittest.mock import AsyncMock, MagicMock, call, patch
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from blackbull.server.sender import WebSocketSender, AsyncioWriter, AbstractWriter
from blackbull.server.recipient import WebSocketRecipient, AsyncioReader, AbstractReader
from blackbull.server.ws_codec import (encode_frame, encode_frame_header,
                                       read_frame, WSOpcode)


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

    def writelines(self, parts):
        # Mirror asyncio.StreamWriter.writelines (vectored write): the
        # concatenated bytes are identical to a single write of the joined
        # parts, so byte-level assertions on ``written`` still hold.
        for part in parts:
            self.written += part

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

    def __init__(self, raw_bytes: bytes, *, max_frame_payload: int | None = None):
        self.writer = _FakeWriter()
        # Wrap the sync _FakeWriter in AsyncioWriter so WebSocketRecipient
        # receives a proper AbstractWriter (shim removed from __init__).
        self._recipient = WebSocketRecipient(
            AsyncioReader(_FakeReader(raw_bytes)),
            AsyncioWriter(self.writer),
            max_frame_payload=max_frame_payload,
        )

    async def receive(self):
        return await self._recipient()


# ---------------------------------------------------------------------------
# _encode_frame (Bug 5 pre-condition)
# ---------------------------------------------------------------------------

class TestEncodeFrame:
    """_encode_frame must produce valid unmasked WebSocket frames."""

    @given(payload=st.binary(min_size=0, max_size=65536),
           opcode=st.sampled_from([0x1, 0x2, 0x8]))
    def test_encode_frame_length_encoding(self, payload, opcode):
        """_encode_frame uses the correct RFC 6455 length encoding for all payload sizes."""
        frame = encode_frame(payload, opcode=opcode)
        length = len(payload)
        assert frame[0] == 0x80 | opcode
        if length < 126:
            assert frame[1] == length
            assert frame[2:] == payload
        elif length < 65536:
            assert frame[1] == 126
            assert int.from_bytes(frame[2:4], 'big') == length
            assert frame[4:] == payload
        else:
            assert frame[1] == 127
            assert int.from_bytes(frame[2:10], 'big') == length
            assert frame[10:] == payload

    def test_binary_opcode(self):
        frame = encode_frame(b'\x00\x01', opcode=0x2)
        assert frame[0] & 0x0F == 0x2

    def test_close_frame_opcode(self):
        frame = encode_frame(b'\x03\xe8', opcode=0x8)
        assert frame[0] & 0x0F == 0x8


class TestEncodeFrameHeader:
    """encode_frame_header must yield the same unmasked header bytes that
    encode_frame prepends — so a vectored ``(header, payload)`` write is
    byte-for-byte identical to the old single ``encode_frame`` frame."""

    @given(payload=st.binary(min_size=0, max_size=70000),
           opcode=st.sampled_from([0x1, 0x2, 0x8]))
    def test_header_plus_payload_equals_encode_frame(self, payload, opcode):
        """The load-bearing invariant behind the P2 vectored-send change:
        header + payload over the wire == encode_frame(payload)."""
        header = encode_frame_header(len(payload), opcode)
        assert header + payload == encode_frame(payload, opcode=opcode)

    def test_short_header_is_two_bytes(self):
        header = encode_frame_header(7, WSOpcode.TEXT)
        assert header == bytes((0x80 | 0x1, 7))

    def test_rsv1_bit_is_set(self):
        header = encode_frame_header(5, WSOpcode.TEXT, rsv1=True)
        assert header[0] & 0x40  # RSV1
        # …and matches encode_frame's own rsv1 header.
        assert header + b'xxxxx' == encode_frame(b'xxxxx', opcode=WSOpcode.TEXT,
                                                 rsv1=True)

    def test_extended_16bit_and_64bit_lengths(self):
        h16 = encode_frame_header(300, WSOpcode.BINARY)
        assert h16[1] == 126 and int.from_bytes(h16[2:4], 'big') == 300
        h64 = encode_frame_header(70000, WSOpcode.BINARY)
        assert h64[1] == 127 and int.from_bytes(h64[2:10], 'big') == 70000


# ---------------------------------------------------------------------------
# _read_frame (Bug 4)
# ---------------------------------------------------------------------------

class TestReadFrame:
    """_read_frame must correctly parse masked client frames per RFC 6455."""

    @given(opcode=st.sampled_from([0x1, 0x2, 0x8]),
           payload=st.binary(min_size=1, max_size=256))
    def test_reads_frame_any_opcode_and_payload(self, opcode, payload):
        """_read_frame must round-trip any opcode+payload combination correctly."""
        async def _run():
            reader = _FakeReader(_make_client_frame(payload, opcode=opcode))
            returned_opcode, got = await read_frame(reader)
            assert returned_opcode == opcode
            assert got == payload
        asyncio.run(_run())

    @pytest.mark.asyncio
    async def test_unmask_is_applied(self):
        """Masked frame: payload must be XOR-unmasked correctly."""
        raw_payload = b'ABCD'
        data = _make_client_frame(raw_payload, opcode=0x1)
        reader = _FakeReader(data)
        _, got = await read_frame(reader)
        assert got == raw_payload

    @pytest.mark.asyncio
    async def test_medium_payload_extended_length(self):
        """Frames with 126-byte payloads use the 16-bit extended-length field."""
        payload = b'z' * 126
        data = _make_client_frame(payload, opcode=0x1)
        reader = _FakeReader(data)
        opcode, got = await read_frame(reader)
        assert got == payload

    @pytest.mark.asyncio
    async def test_incomplete_read_raises(self):
        """Truncated stream must raise IncompleteReadError, not return partial data."""
        reader = _FakeReader(b'\x81')   # only 1 byte – header requires 2
        with pytest.raises(asyncio.IncompleteReadError):
            await read_frame(reader)


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
    """WebSocketRecipient.receive() must reply to ping frames with a pong frame
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
        expected_pong = encode_frame(ping_payload, opcode=0xA)
        assert handler.writer.written == expected_pong

    async def test_pong_payload_matches_ping_payload(self):
        """Pong payload must be identical to ping payload (RFC 6455 §5.5.2).

        The server also echoes a CLOSE frame in reply to the trailing CLOSE
        (RFC §5.5.1), so the wire is ``pong + close_echo``.
        """
        ping_payload = b'\x01\x02\x03\x04'
        frames = (_make_client_frame(ping_payload, opcode=0x9)
                  + _make_client_frame(b'', opcode=0x8))  # close to end loop
        handler = self._make_handler(frames)
        await handler.receive()       # connect
        await handler.receive()       # ping → pong, then close → disconnect
        pong = encode_frame(ping_payload, opcode=0xA)
        close_echo = encode_frame((1000).to_bytes(2, 'big'), opcode=0x8)
        assert handler.writer.written == pong + close_echo

    async def test_empty_ping_causes_empty_pong(self):
        """Ping with empty payload must produce a pong with empty payload."""
        frames = (_make_client_frame(b'', opcode=0x9)
                  + _make_client_frame(b'hi', opcode=0x1))
        handler = self._make_handler(frames)
        await handler.receive()       # connect
        await handler.receive()       # empty ping → empty pong, then text
        expected_pong = encode_frame(b'', opcode=0xA)
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
        pong1 = encode_frame(p1, opcode=0xA)
        pong2 = encode_frame(p2, opcode=0xA)
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
    """WebSocketRecipient.receive() must reject unmasked client frames.

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

    @given(opcode=st.sampled_from([0x1, 0x2, 0x8, 0x9]),
           payload=st.binary(max_size=32))
    def test_unmasked_frame_raises_for_any_opcode(self, opcode, payload):
        """Any unmasked client frame must be rejected, regardless of opcode or payload."""
        async def _run():
            handler = self._make_handler(_make_unmasked_frame(payload, opcode=opcode))
            await handler.receive()    # consume websocket.connect
            with pytest.raises(Exception):
                await handler.receive()
        asyncio.run(_run())

    async def test_masked_frame_still_accepted(self):
        """Properly masked frame must continue to be accepted (regression)."""
        handler = self._make_handler(_make_client_frame(b'hello', opcode=0x1))
        await handler.receive()       # connect
        event = await handler.receive()
        assert event == {'type': 'websocket.receive', 'text': 'hello', 'bytes': None}


# ---------------------------------------------------------------------------
# Unknown opcode handling (P1 §5.2)
# ---------------------------------------------------------------------------

_CLOSE_1002 = encode_frame((1002).to_bytes(2, 'big'), opcode=0x8)


@pytest.mark.asyncio
class TestUnknownOpcodeHandling:
    """RFC 6455 §5.2: unknown/reserved opcode must send CLOSE(1002) and fail the connection.

    P1 bug: the previous ``case _:`` branch logged a warning and delivered the
    frame as ``websocket.receive``, silently violating the spec.
    """

    def _make_handler(self, raw_bytes: bytes):
        return _RecipientWrapper(raw_bytes)

    async def test_unknown_opcode_sends_close_1002(self):
        """Reserved opcode 0x03 must cause a CLOSE(1002) frame to be written to the wire."""
        frame = _make_client_frame(b'data', opcode=0x03)
        handler = self._make_handler(frame)
        await handler.receive()         # consume websocket.connect
        await handler.receive()         # triggers unknown-opcode path
        assert _CLOSE_1002 in bytes(handler.writer.written)

    async def test_unknown_opcode_disconnects_not_receive(self):
        """Unknown opcode must produce websocket.disconnect, not websocket.receive."""
        frame = _make_client_frame(b'data', opcode=0x03)
        handler = self._make_handler(frame)
        await handler.receive()         # connect
        event = await handler.receive()
        assert event['type'] == 'websocket.disconnect'
        assert event['code'] == 1002

    async def test_unknown_opcode_disconnect_code_is_1002(self):
        """Disconnect code for unknown opcode must be 1002 (protocol error)."""
        frame = _make_client_frame(b'', opcode=0x05)  # another reserved opcode
        handler = self._make_handler(frame)
        await handler.receive()
        event = await handler.receive()
        assert event.get('code') == 1002


@pytest.mark.asyncio
class TestUnmaskedFrameSendsClose:
    """RFC 6455 §5.1 + §7.2: unmasked frame must send CLOSE(1002) before failing.

    P1 bug: the previous implementation raised the exception without writing
    a CLOSE frame to the wire first.
    """

    def _make_handler(self, raw_bytes: bytes):
        return _RecipientWrapper(raw_bytes)

    async def test_unmasked_frame_sends_close_1002(self):
        """CLOSE(1002) must appear on the wire when an unmasked frame is received."""
        handler = self._make_handler(_make_unmasked_frame(b'hello', opcode=0x1))
        await handler.receive()         # connect
        with pytest.raises(Exception):
            await handler.receive()
        assert _CLOSE_1002 in bytes(handler.writer.written)

    async def test_unmasked_binary_frame_sends_close_1002(self):
        """Same for binary opcode."""
        handler = self._make_handler(_make_unmasked_frame(b'\x00', opcode=0x2))
        await handler.receive()
        with pytest.raises(Exception):
            await handler.receive()
        assert _CLOSE_1002 in bytes(handler.writer.written)


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
        expected_pong = encode_frame(ping_payload, opcode=0xA)
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

    async def test_deflate_frame_rejected_without_negotiated_extension(self):
        """RSV1=1 frame MUST be rejected when permessage-deflate wasn't negotiated.

        BlackBull does not advertise Sec-WebSocket-Extensions, so any RSV
        bit set on an incoming frame is a protocol error per RFC 6455 §5.2.
        """
        payload = b'Hello, compressed world!'
        handler = _RecipientWrapper(self._deflate_frame(payload))
        await handler.receive()  # websocket.connect
        with pytest.raises(Exception):
            await handler.receive()

    async def test_uncompressed_frame_unaffected_by_deflate_support(self):
        """A normal (RSV1=0) frame must pass through unchanged."""
        handler = _RecipientWrapper(_make_client_frame(b'plain', opcode=0x1))
        await handler.receive()  # websocket.connect
        event = await handler.receive()
        assert event.get('text') == 'plain', (
            f"Uncompressed frame must not be decompressed; got {event!r}"
        )


# ---------------------------------------------------------------------------
# Payload-size guard (post-handshake OOM defence)
# ---------------------------------------------------------------------------
#
# RFC 6455 §5.2 allows declared frame payload lengths up to 2**63 - 1.
# Without a cap, an adversary post-handshake can advertise a 2**63 - 1
# payload in the frame header; ``read_payload`` then attempts to buffer
# the full declared length and the server OOMs before any body bytes
# arrive.  ``WebSocketRecipient._MAX_FRAME_PAYLOAD`` caps the accepted
# declared length at 1 MiB by default (constructor-overridable) and
# raises ``ProtocolError(close_code=MESSAGE_TOO_BIG)`` which the read
# loop converts into a CLOSE(1009) frame.

class TestFramePayloadSizeGuard:
    """Frame header declaring a payload larger than
    ``_MAX_FRAME_PAYLOAD`` must close the connection with code 1009
    (MESSAGE_TOO_BIG) before any body bytes are read."""

    @staticmethod
    def _frame_with_declared_length(declared_length: int,
                                    actual_payload: bytes,
                                    mask: bytes = b'\x00\x00\x00\x00') -> bytes:
        """Build a masked client frame whose 64-bit extended-length
        field advertises *declared_length* bytes, regardless of what
        *actual_payload* contains.  The server must reject on header
        parse alone — no need to send a real megabyte body."""
        # FIN=1, opcode=BINARY
        b0 = 0x82
        # MASK=1, length=127 (extended 64-bit follows)
        b1 = 0x80 | 127
        return (bytes([b0, b1])
                + declared_length.to_bytes(8, 'big')
                + mask
                + actual_payload)

    @pytest.mark.asyncio
    async def test_oversize_declared_length_triggers_close_1009(self):
        from blackbull.server.constants import WSCloseCode
        # Pin an explicit 64 KiB cap for the test — the global default
        # is 64 MiB (Sprint 43, BB_WS_MAX_FRAME_PAYLOAD).  Asserting the
        # rejection mechanism doesn't need the default to be small; pinning
        # in-test keeps this assertion stable across default changes.
        # Declare 1 MiB payload but supply only 16 actual bytes — the cap
        # is checked on the declared length before read_payload runs.
        raw = self._frame_with_declared_length(
            declared_length=1 * 1024 * 1024,
            actual_payload=b'\x00' * 16,
        )
        handler = _RecipientWrapper(raw, max_frame_payload=64 * 1024)
        await handler.receive()  # synthetic websocket.connect

        # The next call surfaces the ProtocolError raised inside _read_loop;
        # by then the CLOSE(1009) frame has already been written.
        with pytest.raises(Exception):
            await handler.receive()

        written = bytes(handler.writer.written)
        assert written, 'server must send a CLOSE frame on oversized payload'
        # CLOSE frames: byte 0 = 0x88, byte 1 = length (no mask from
        # server) + 0x80 if masked (client-mode wrapper masks).
        # The payload starts with the 2-byte status code in big-endian.
        # Find the status-code bytes by scanning past header + mask.
        # Status code is the first two bytes of the unmasked close payload.
        # Easier: just assert MESSAGE_TOO_BIG (1009 = 0x03F1) appears in the
        # outbound bytes — the masking would XOR but the test wrapper uses
        # an all-zero mask so the payload is unchanged.
        assert WSCloseCode.MESSAGE_TOO_BIG.to_bytes(2, 'big') in written, (
            f'CLOSE frame must carry status code 1009 (MESSAGE_TOO_BIG); '
            f'wire bytes: {written!r}')

    @pytest.mark.asyncio
    async def test_legitimate_small_frame_unaffected(self):
        """Sanity: a normal small frame still works.  Regression lock
        against an overly-aggressive cap implementation."""
        handler = _RecipientWrapper(_make_client_frame(b'hello', opcode=0x1))
        await handler.receive()  # websocket.connect
        event = await handler.receive()
        assert event.get('text') == 'hello'

    @pytest.mark.asyncio
    async def test_constructor_override_raises_cap(self):
        """A caller with known-large workloads can opt into a higher
        cap via the ``max_frame_payload`` constructor parameter
        without subclassing."""
        raw = self._frame_with_declared_length(
            declared_length=2 * 1024 * 1024,
            actual_payload=b'\x00' * 16,
        )
        wrapper = _RecipientWrapper.__new__(_RecipientWrapper)
        wrapper.writer = _FakeWriter()
        wrapper._recipient = WebSocketRecipient(
            AsyncioReader(_FakeReader(raw)),
            AsyncioWriter(wrapper.writer),
            max_frame_payload=4 * 1024 * 1024,  # 4 MiB — well above
        )
        await wrapper._recipient()  # connect
        # Cap is 4 MiB; declared 2 MiB is under the override.  The
        # frame will eventually fail elsewhere (insufficient body bytes
        # off the stream), but NOT with the size-guard ProtocolError.
        # Confirm the writer did NOT receive a 1009 CLOSE.
        from blackbull.server.constants import WSCloseCode
        try:
            await wrapper._recipient()
        except Exception:
            pass  # expected — fake reader runs out of bytes; we assert on the writer, not the exception
        assert WSCloseCode.MESSAGE_TOO_BIG.to_bytes(2, 'big') not in bytes(wrapper.writer.written), (
            'with the cap raised, the size guard must not fire on '
            'declared lengths within the override')
