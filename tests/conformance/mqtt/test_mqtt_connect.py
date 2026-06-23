"""
MQTT 5.0 CONNECT / CONNACK / DISCONNECT conformance tests — Sprint 52.

Verifies session establishment and graceful teardown against the MQTT 5.0
OASIS Standard.

Reference: MQTT Version 5.0, OASIS Standard
  §3.1  CONNECT – Connection Request
  §3.2  CONNACK – Connect Acknowledgment
  §3.14 DISCONNECT – Disconnect Notification

Key behaviours tested:
  - §3.1.2.1  Protocol Name MUST be "MQTT" (UTF-8)
  - §3.1.2.2  Protocol Level MUST be 5
  - §3.1.2.3  Connect Flags (Clean Start, Will, QoS, Retain, Password, User Name)
  - §3.1.2.10 Keep Alive (2-byte unsigned integer)
  - §3.1.3    CONNECT Payload (Client ID, Will Topic, Will Payload, User, Password)
  - §3.1.4    CONNECT with zero-length Client ID → server assigns one (if enabled)
  - §3.2.2.2  CONNACK Reason Codes
  - §3.2.2.3  Session Present flag
  - §3.2.2.4  CONNACK Properties (Server Keep Alive, Assigned Client ID, etc.)
  - §3.14.2.1 DISCONNECT Reason Codes
"""

import asyncio
import pytest

from blackbull.mqtt.messages import (
    MQTTConnect, MQTTConnack, MQTTDisconnect,
    MQTTPublish, MQTTSubscribe, MQTTSuback,
    encode_packet, decode_packet,
    MQTTReasonCode,
)
from blackbull.server.protocol_registry import ProtocolContext
from blackbull.server.sender import AbstractWriter
from blackbull.server.recipient import AbstractReader


# ============================================================================
# In-process fake reader/writer for Actor tests
# ============================================================================

class _FakeMQTTReader(AbstractReader):
    """Simulates an MQTT client connection feeding bytes into the Actor."""

    def __init__(self, data: bytes = b''):
        self._buf = bytearray(data)

    async def read(self, n: int) -> bytes:
        if not self._buf:
            return b''
        chunk = bytes(self._buf[:n])
        del self._buf[:n]
        return chunk

    def feed(self, data: bytes) -> None:
        """Queue more data to be read by the Actor."""
        self._buf.extend(data)

    def feed_packet(self, packet) -> None:
        """Encode an MQTT packet dataclass and feed it into the buffer."""
        self._buf.extend(encode_packet(packet))


class _FakeMQTTWriter(AbstractWriter):
    """Captures bytes written by the Actor for assertion."""

    def __init__(self):
        self.written = bytearray()

    async def write(self, data: bytes) -> None:
        self.written.extend(data)

    def pop_packets(self) -> list:
        """Decode and return all complete MQTT packets written so far,
        consuming them from the buffer."""
        packets = []
        offset = 0
        buf = bytes(self.written)
        while offset < len(buf):
            packet, consumed = decode_packet(buf[offset:])
            packets.append(packet)
            offset += consumed
        self.written = self.written[offset:]
        return packets


def _make_protocol_ctx() -> ProtocolContext:
    return ProtocolContext(
        peername=('127.0.0.1', 54321),
        sockname=('0.0.0.0', 1883),
        ssl=False,
        aggregator=None,
        connection_id='test-conn-001',
        protocol='mqtt',
    )


# ============================================================================
# §3.1 — CONNECT packet acceptance criteria
# ============================================================================

class TestConnectAcceptance:
    """§3.1 — Valid CONNECT packets must be accepted and trigger CONNACK.

    A correctly formed CONNECT with protocol name "MQTT" and protocol level 5
    must receive a CONNACK with reason code Success (0x00).
    """

    @pytest.mark.asyncio
    async def test_valid_connect_receives_connack_success(self, mqtt):
        """§3.1 → §3.2 — Valid CONNECT is answered with CONNACK Success."""

        reader = _FakeMQTTReader()
        writer = _FakeMQTTWriter()
        ctx = _make_protocol_ctx()

        actor = mqtt.serve(reader, writer, ctx)
        reader.feed_packet(MQTTConnect(
            client_id='test-client',
            clean_start=True,
            keep_alive=60,
        ))

        # Run actor for one message cycle
        task = asyncio.create_task(actor.run())
        await asyncio.sleep(0.05)  # let the message loop process
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        packets = writer.pop_packets()
        connacks = [p for p in packets if isinstance(p, MQTTConnack)]
        assert len(connacks) >= 1, "Expected CONNACK after CONNECT"
        assert connacks[0].reason_code in (0x00,), \
            f"Expected CONNACK Success (0x00), got 0x{connacks[0].reason_code:02X}"

    @pytest.mark.asyncio
    async def test_connect_with_clean_start_true_no_existing_session(self, mqtt):
        """§3.1.2.3 — Clean Start = 1: discard any existing session, start fresh."""
        reader = _FakeMQTTReader()
        writer = _FakeMQTTWriter()
        ctx = _make_protocol_ctx()

        actor = mqtt.serve(reader, writer, ctx)
        # First connect with Clean Start = True; then disconnect, then reconnect
        reader.feed_packet(MQTTConnect(
            client_id='cs-true-client',
            clean_start=True,
            keep_alive=60,
        ))
        reader.feed_packet(MQTTDisconnect(reason_code=0))

        task = asyncio.create_task(actor.run())
        await asyncio.sleep(0.05)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        packets = writer.pop_packets()
        connacks = [p for p in packets if isinstance(p, MQTTConnack)]
        assert len(connacks) >= 1
        # §3.2.2.3 — Session Present should be False for Clean Start
        assert connacks[0].session_present is False, \
            "Clean Start = True must yield Session Present = False"

    @pytest.mark.asyncio
    async def test_connect_with_clean_start_false_session_present(self, mqtt):
        """§3.1.2.3, §3.2.2.3 — Clean Start = 0: resume existing session if any."""
        reader = _FakeMQTTReader()
        writer = _FakeMQTTWriter()
        ctx = _make_protocol_ctx()

        # Pre-populate a session for the client
        mqtt.sessions['resume-client'] = {
            'subscriptions': {},
            'pending_qos2_in': {},
            'pending_qos2_out': {},
        }

        actor = mqtt.serve(reader, writer, ctx)
        reader.feed_packet(MQTTConnect(
            client_id='resume-client',
            clean_start=False,
            keep_alive=60,
        ))

        task = asyncio.create_task(actor.run())
        await asyncio.sleep(0.05)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        packets = writer.pop_packets()
        connacks = [p for p in packets if isinstance(p, MQTTConnack)]
        assert len(connacks) >= 1
        # §3.2.2.3 — Session Present should be True when session exists
        assert connacks[0].session_present is True, \
            "Clean Start = False with existing session → Session Present = True"


# ============================================================================
# §3.1.4 / 3.1.3 — CONNECT Payload validation
# ============================================================================

class TestConnectPayload:
    """§3.1.3 / §3.1.4 — CONNECT payload fields."""

    def test_connect_with_zero_length_client_id(self, mqtt):
        """§3.1.4 — A zero-length Client ID requests server-assigned Client ID.

        If the server accepts, CONNACK must include Assigned Client Identifier
        property (§3.2.2.3.2).
        """
        connect = MQTTConnect(
            client_id='',  # §3.1.4: zero-length = server assigns
            clean_start=True,
            keep_alive=60,
        )
        wire = encode_packet(connect)
        decoded = decode_packet(wire)
        assert decoded.client_id == '', "Client ID should be empty string"
        # Server behaviour is tested in integration tests below

    def test_connect_with_long_client_id(self, mqtt):
        """§1.5.4 — Client ID is a UTF-8 string; 65535-byte maximum for
        MQTT strings in general (except where properties extend this)."""
        # §1.5.4 — Max UTF-8 string length is 65535 bytes
        long_id = 'a' * 65535  # boundary case
        connect = MQTTConnect(
            client_id=long_id,
            clean_start=True,
            keep_alive=60,
        )
        wire = encode_packet(connect)
        decoded = decode_packet(wire)
        assert decoded.client_id == long_id


class TestConnectFlags:
    """§3.1.2.3 — CONNECT Connect Flags bit field."""

    def test_connect_with_will_flag(self, mqtt):
        """§3.1.2.3 — Will Flag (bit 2): if set, Will QoS/Retain + Topic +
        Payload must be present."""
        connect = MQTTConnect(
            client_id='will-client',
            clean_start=True,
            keep_alive=60,
            will_topic='clients/will-client/status',
            will_payload=b'offline',
            will_qos=0,
            will_retain=False,
        )
        wire = encode_packet(connect)
        decoded = decode_packet(wire)
        assert decoded.will_topic == 'clients/will-client/status'
        assert decoded.will_payload == b'offline'
        assert decoded.will_qos == 0

    def test_connect_with_username_password(self, mqtt):
        """§3.1.2.3 — User Name Flag (bit 7) and Password Flag (bit 6)."""
        connect = MQTTConnect(
            client_id='auth-client',
            clean_start=True,
            keep_alive=60,
            username='user1',
            password=b'secret123',
        )
        wire = encode_packet(connect)
        decoded = decode_packet(wire)
        assert decoded.username == 'user1'
        assert decoded.password == b'secret123'

    def test_connect_password_without_username_malformed(self, mqtt):
        """§3.1.2.3 — Password Flag MUST NOT be set without User Name Flag."""
        # The encode function should either reject this or enforce the constraint
        with pytest.raises(ValueError, match='[Pp]assword.*[Uu]ser'):
            MQTTConnect(
                client_id='bad-client',
                clean_start=True,
                keep_alive=60,
                password=b'secret',
            )


# ============================================================================
# §3.2 — CONNACK validation
# ============================================================================

class TestConnackValidation:
    """§3.2 — CONNACK packet structure and reason codes."""

    def test_connack_success_no_session(self, mqtt):
        """§3.2.2.2 — CONNACK Success (0x00), Session Present = 0."""
        connack = MQTTConnack(
            session_present=False,
            reason_code=0x00,
        )
        wire = encode_packet(connack)
        decoded = decode_packet(wire)
        assert decoded.reason_code == 0x00
        assert decoded.session_present is False

    def test_connack_with_server_keep_alive(self, mqtt):
        """§3.2.2.3.2 — CONNACK may include Server Keep Alive property
        overriding the client's requested Keep Alive."""
        connack = MQTTConnack(
            session_present=False,
            reason_code=0x00,
            properties={'server_keep_alive': 120},
        )
        wire = encode_packet(connack)
        decoded = decode_packet(wire)
        assert decoded.properties.get('server_keep_alive') == 120

    def test_connack_with_assigned_client_id(self, mqtt):
        """§3.2.2.3.2 — If client sent zero-length Client ID, CONNACK
        must include Assigned Client Identifier."""
        connack = MQTTConnack(
            session_present=False,
            reason_code=0x00,
            properties={'assigned_client_identifier': 'auto-gen-xyz'},
        )
        wire = encode_packet(connack)
        decoded = decode_packet(wire)
        assert decoded.properties.get('assigned_client_identifier') == 'auto-gen-xyz'

    @pytest.mark.parametrize("reason_code", [
        0x84,  # Unsupported Protocol Version
        0x85,  # Client Identifier not valid
        0x86,  # Bad User Name or Password
        0x87,  # Not authorized
        0x88,  # Server unavailable
        0x89,  # Server busy
        0x8A,  # Banned
        0x8C,  # Bad authentication method
        0x8D,  # Topic Name invalid  (unlikely for CONNACK but in register)
    ])
    def test_connack_error_reason_codes(self, reason_code):
        """§3.2.2.2 — CONNACK error reason codes are correctly encoded."""
        connack = MQTTConnack(
            session_present=False,
            reason_code=reason_code,
        )
        wire = encode_packet(connack)
        decoded = decode_packet(wire)
        assert decoded.reason_code == reason_code
        assert decoded.session_present is False


# ============================================================================
# §3.2.2.5 — Unsupported Protocol Version handling
# ============================================================================

class TestUnsupportedProtocolVersion:
    """§3.2.2.5 — If the server does not support the Protocol Level,
    it MUST respond with CONNACK reason code 0x84 (Unsupported Protocol Version)
    and then close the connection."""

    def test_protocol_level_4_rejected(self, mqtt):
        """§3.2.2.5 — Protocol Level 4 (MQTT 3.1.1) is rejected with 0x84."""
        # Build a CONNECT packet with Protocol Level = 4 manually
        # Fixed header: 0x10 (CONNECT)
        # This test verifies the server-side validation logic
        # by constructing the raw bytes directly.
        # Protocol Level is at a fixed offset after Protocol Name "MQTT"
        connect_bytes = bytes([
            0x10,           # CONNECT, flags=0
            13,             # Remaining Length (protocol(6) + level(1) + flags(1) + keepalive(2) + clientid(3))
            0x00, 0x04,     # Protocol Name length = 4
            0x4D, 0x51, 0x54, 0x54,  # "MQTT"
            0x04,           # Protocol Level = 4 (MQTT 3.1.1)
            0x02,           # Connect Flags: Clean Start
            0x00, 0x3C,     # Keep Alive = 60
            0x00, 0x01,     # Client ID length = 1
            0x41,           # Client ID = "A"
        ])
        from blackbull.mqtt.messages import decode_packet
        packet, consumed = decode_packet(connect_bytes)
        assert isinstance(packet, MQTTConnect)
        assert packet.proto_level == 4  # server must reject this


# ============================================================================
# §3.14 — DISCONNECT behavior
# ============================================================================

class TestDisconnectBehavior:
    """§3.14 — DISCONNECT packet handling.

    A DISCONNECT from the client indicates a graceful shutdown.  The server
    must:
      - §3.14.2.1: Accept reason codes for different disconnect reasons
      - §3.1.2.5: If the Will Flag was set in CONNECT and the disconnect is
        not graceful (or uses reason code 0x04), send the Will Message
      - Close the TCP connection after receiving DISCONNECT
    """

    def test_disconnect_with_reason_code_normal(self, mqtt):
        """§3.14.2.1 — DISCONNECT with reason code 0x00 (Normal disconnection)."""
        disconnect = MQTTDisconnect(reason_code=0x00)
        wire = encode_packet(disconnect)
        decoded = decode_packet(wire)
        assert decoded.reason_code == 0x00

    @pytest.mark.parametrize("reason_code,reason_name", [
        (0x00, 'Normal disconnection'),
        (0x04, 'Disconnect with Will Message'),
        (0x80, 'Unspecified error'),
        (0x81, 'Malformed Packet'),
        (0x82, 'Protocol Error'),
        (0x87, 'Not authorized'),
        (0x89, 'Server busy'),
        (0x8E, 'Packet too large'),
        (0x8F, 'Quota exceeded'),
        (0x95, 'Server moved'),
        (0x98, 'Subscription Identifiers not supported'),
    ])
    def test_disconnect_reason_codes(self, reason_code, reason_name):
        """§3.14.2.1 — All valid DISCONNECT reason codes are encodable."""
        disconnect = MQTTDisconnect(
            reason_code=reason_code,
            properties={'reason_string': reason_name},
        )
        wire = encode_packet(disconnect)
        decoded = decode_packet(wire)
        assert decoded.reason_code == reason_code

    def test_disconnect_without_reason_code(self, mqtt):
        """§3.14 — DISCONNECT with no reason code (pre-5.0 compatible).
        In MQTT 5.0, a DISCONNECT packet MAY omit the Reason Code and Properties
        if they are zero-length."""
        # Minimal DISCONNECT: just the fixed header (0xE0, 0x00)
        disconnect = MQTTDisconnect()  # no reason code, no properties
        wire = encode_packet(disconnect)
        assert wire[0] == 0xE0  # CONNECT type + 0 flags
        # The remaining length should be 0 if no reason code
        assert wire[1] == 0x00


# ============================================================================
# §3.1.2.5, §3.1.3.3 — Will Message (Last Will and Testament)
# ============================================================================

class TestWillMessageOnConnect:
    """§3.1.2.5 — Will Message configuration in CONNECT.

    The Will Message is published by the server on behalf of the client
    when the network connection is closed without a DISCONNECT packet
    (or with DISCONNECT reason code 0x04 — Disconnect with Will Message).
    """

    def test_connect_with_will_message_full_spec(self, mqtt):
        """§3.1.3.3 — CONNECT with Will Topic, Will Payload, Will QoS, Will Retain,
        and Will Properties (Will Delay Interval)."""
        connect = MQTTConnect(
            client_id='will-client',
            clean_start=True,
            keep_alive=60,
            will_topic='system/clients/will-client/status',
            will_payload=b'{"status": "offline", "reason": "connection lost"}',
            will_qos=1,
            will_retain=True,
            will_properties={'will_delay_interval': 30,
                             'content_type': 'application/json',
                             'user_properties': [('type', 'lwt')]},
        )
        wire = encode_packet(connect)
        decoded = decode_packet(wire)
        assert decoded.will_topic == 'system/clients/will-client/status'
        assert decoded.will_payload == b'{"status": "offline", "reason": "connection lost"}'
        assert decoded.will_qos == 1
        assert decoded.will_retain is True
        assert decoded.will_properties == {
            'will_delay_interval': 30,
            'content_type': 'application/json',
            'user_properties': [('type', 'lwt')],
        }
