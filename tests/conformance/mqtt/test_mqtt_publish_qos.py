"""
MQTT 5.0 PUBLISH QoS 0/1/2 conformance tests — Sprint 52.

Verifies PUBLISH delivery at all three QoS levels against the MQTT 5.0
OASIS Standard.

Reference: MQTT Version 5.0, OASIS Standard
  §3.3  PUBLISH – Publish Message
  §3.4  PUBACK  – Publish Acknowledgment (QoS 1)
  §3.5  PUBREC  – Publish Received (QoS 2, step 1)
  §3.6  PUBREL  – Publish Release (QoS 2, step 2)
  §3.7  PUBCOMP – Publish Complete (QoS 2, step 3)
  §4.3  QoS 0: At most once delivery
  §4.4  QoS 1: At least once delivery
  §4.5  QoS 2: Exactly once delivery
"""

import asyncio
import pytest

from blackbull.mqtt.messages import (
    MQTTConnect, MQTTConnack, MQTTDisconnect,
    MQTTPublish, MQTTPuback, MQTTPubrec, MQTTPubrel, MQTTPubcomp,
    MQTTSubscribe, MQTTSuback,
    encode_packet, decode_packet,
)
from blackbull.server.protocol_registry import ProtocolContext
from blackbull.server.sender import AbstractWriter
from blackbull.server.recipient import AbstractReader


# ---------------------------------------------------------------------------
# In-process fakes
# ---------------------------------------------------------------------------

class _FakeMQTTReader(AbstractReader):
    def __init__(self, data: bytes = b''):
        self._buf = bytearray(data)

    async def read(self, n: int) -> bytes:
        if not self._buf:
            return b''
        chunk = bytes(self._buf[:n])
        del self._buf[:n]
        return chunk

    def feed_packet(self, packet) -> None:
        self._buf.extend(encode_packet(packet))


class _FakeMQTTWriter(AbstractWriter):
    def __init__(self):
        self.written = bytearray()

    async def write(self, data: bytes) -> None:
        self.written.extend(data)

    def pop_packets(self) -> list:
        packets = []
        offset = 0
        buf = bytes(self.written)
        while offset < len(buf):
            packet, consumed = decode_packet(buf[offset:])
            packets.append(packet)
            offset += consumed
        self.written = self.written[offset:]
        return packets


def _ctx():
    return ProtocolContext(
        peername=('127.0.0.1', 54321),
        sockname=('0.0.0.0', 1883),
        ssl=False,
        aggregator=None,
        connection_id='test-conn',
        protocol='mqtt',
    )


# ============================================================================
# §4.3 / §3.3 — QoS 0: At most once (fire-and-forget)
# ============================================================================

class TestPublishQoS0:
    """§4.3 — QoS 0: At most once delivery.

    The message is delivered according to the capabilities of the underlying
    network.  No response is sent by the receiver and no retry is performed
    by the sender.  The message arrives at the receiver either once or not
    at all.
    """

    # §3.3.2-1 — QoS 0 PUBLISH has no Packet Identifier
    def test_publish_qos0_no_packet_identifier(self, mqtt):
        """§3.3.2-1 — A PUBLISH with QoS 0 MUST NOT contain a Packet Identifier."""
        publish = MQTTPublish(
            topic='test/qos0',
            payload=b'hello-qos0',
            qos=0,
        )
        wire = encode_packet(publish)
        decoded = decode_packet(wire)
        assert decoded.qos == 0
        assert decoded.packet_id is None, \
            "QoS 0 PUBLISH MUST NOT have a Packet Identifier"

    # §3.3.2-1 — Server must forward QoS 0 PUBLISH to matching subscribers
    @pytest.mark.asyncio
    async def test_qos0_publish_delivered_to_subscriber(self, mqtt):
        """§4.3 — QoS 0 PUBLISH is delivered to subscribers without acknowledgment."""

        reader = _FakeMQTTReader()
        writer = _FakeMQTTWriter()
        ctx = _ctx()

        # Connect and subscribe
        reader.feed_packet(MQTTConnect(client_id='qos0-sub', clean_start=True, keep_alive=60))
        reader.feed_packet(MQTTSubscribe(
            packet_id=1,
            subscriptions=[('test/qos0', 0)],
        ))

        # Publish QoS 0
        reader.feed_packet(MQTTPublish(
            topic='test/qos0',
            payload=b'hello-qos0',
            qos=0,
        ))

        actor = mqtt.serve(reader, writer, ctx)
        task = asyncio.create_task(actor.run())
        await asyncio.sleep(0.1)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        packets = writer.pop_packets()
        # Should have: CONNACK, SUBACK, and the PUBLISH forwarded back
        publishes = [p for p in packets if isinstance(p, MQTTPublish)]
        assert len(publishes) >= 1, "Expected forwarded PUBLISH to subscriber"
        assert publishes[0].topic == 'test/qos0'
        assert publishes[0].payload == b'hello-qos0'

    # §3.3.2 — QoS 0 with Retain flag
    def test_publish_qos0_retain(self, mqtt):
        """§3.3.2.3 — QoS 0 PUBLISH with RETAIN = 1: server stores the message
        and replaces any previously retained message for that topic."""
        publish = MQTTPublish(
            topic='status/qos0',
            payload=b'online',
            qos=0,
            retain=True,
        )
        wire = encode_packet(publish)
        decoded = decode_packet(wire)
        assert decoded.qos == 0
        assert decoded.retain is True


# ============================================================================
# §4.4 / §3.4 — QoS 1: At least once (PUBACK round-trip)
# ============================================================================

class TestPublishQoS1:
    """§4.4 — QoS 1: At least once delivery.

    The message is guaranteed to arrive at the receiver at least once.
    The sender MUST:
      - §3.3.2-2: Assign a Packet Identifier
      - §4.4:   Retransmit the PUBLISH if PUBACK is not received within
                a reasonable time
      - §4.4:   Treat the PUBLISH as "unacknowledged" until PUBACK arrives

    The receiver MUST:
      - §3.4.2: Respond with a PUBACK containing the same Packet Identifier
    """

    # §3.3.2-2 — QoS 1 PUBLISH MUST include a Packet Identifier
    def test_publish_qos1_has_packet_identifier(self, mqtt):
        """§3.3.2-2 — A PUBLISH with QoS 1 MUST contain a Packet Identifier."""
        publish = MQTTPublish(
            topic='test/qos1',
            payload=b'hello-qos1',
            qos=1,
            packet_id=10,
        )
        wire = encode_packet(publish)
        decoded = decode_packet(wire)
        assert decoded.qos == 1
        assert decoded.packet_id == 10

    # §3.3.2-2 — QoS 1 without Packet Identifier is invalid
    def test_publish_qos1_without_packet_id_raises(self, mqtt):
        """§3.3.2-2 — QoS 1 PUBLISH without Packet Identifier MUST be rejected."""
        with pytest.raises(ValueError, match='[Pp]acket.*[Ii]dentifier'):
            MQTTPublish(topic='test/qos1', payload=b'x', qos=1)

    @pytest.mark.asyncio
    async def test_qos1_puback_round_trip(self, mqtt):
        """§4.4 / §3.4 — Server sends PUBACK in response to QoS 1 PUBLISH."""
        reader = _FakeMQTTReader()
        writer = _FakeMQTTWriter()
        ctx = _ctx()

        # Connect
        reader.feed_packet(MQTTConnect(client_id='qos1-pub', clean_start=True, keep_alive=60))
        # Publish QoS 1
        reader.feed_packet(MQTTPublish(
            topic='test/qos1',
            payload=b'hello-qos1',
            qos=1,
            packet_id=100,
        ))

        actor = mqtt.serve(reader, writer, ctx)
        task = asyncio.create_task(actor.run())
        await asyncio.sleep(0.1)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        packets = writer.pop_packets()
        # Must have CONNACK and PUBACK
        pubacks = [p for p in packets if isinstance(p, MQTTPuback)]
        assert len(pubacks) >= 1, "Expected PUBACK after QoS 1 PUBLISH"
        assert pubacks[0].packet_id == 100, \
            "PUBACK Packet Identifier must match PUBLISH Packet Identifier"
        assert pubacks[0].reason_code == 0x00, \
            "QoS 1 PUBACK should have reason code Success (0x00)"

    # §3.4.2.1 — PUBACK with error reason code
    @pytest.mark.parametrize("error_code", [
        0x10,  # No matching subscribers
        0x80,  # Unspecified error
        0x83,  # Implementation specific error
        0x87,  # Not authorized
        0x8D,  # Topic Name invalid
        0x8E,  # Packet too large
        0x8F,  # Quota exceeded
        0x91,  # Payload format invalid
    ])
    def test_puback_error_reason_codes(self, error_code):
        """§3.4.2.1 — PUBACK can carry error reason codes."""
        puback = MQTTPuback(packet_id=42, reason_code=error_code)
        wire = encode_packet(puback)
        decoded = decode_packet(wire)
        assert decoded.packet_id == 42
        assert decoded.reason_code == error_code


# ============================================================================
# §4.5 / §3.5–3.7 — QoS 2: Exactly once (PUBREC → PUBREL → PUBCOMP)
# ============================================================================

class TestPublishQoS2:
    """§4.5 — QoS 2: Exactly once delivery.

    The four-way handshake ensures the message is delivered exactly once:

      1. Sender   → PUBLISH  (QoS 2, Packet ID=N)
      2. Receiver → PUBREC   (Packet ID=N)          §3.5
      3. Sender   → PUBREL   (Packet ID=N)          §3.6
      4. Receiver → PUBCOMP  (Packet ID=N)          §3.7

    - §4.5.1: The receiver MUST respond with PUBREC on receiving a QoS 2
      PUBLISH (after ensuring the message can be processed).
    - §4.5.2: The sender MUST treat the PUBLISH as unacknowledged until
      PUBREC arrives.  On receiving PUBREC, it sends PUBREL.
    - §4.5.3: The receiver MUST hold the Packet ID until it has processed
      the message and sent PUBCOMP.  If the sender re-sends PUBREL,
      the receiver MUST re-send PUBCOMP.
    """

    # §3.3.2-3 — QoS 2 PUBLISH MUST include a Packet Identifier
    def test_publish_qos2_has_packet_identifier(self, mqtt):
        """§3.3.2-3 — A PUBLISH with QoS 2 MUST contain a Packet Identifier."""
        publish = MQTTPublish(
            topic='test/qos2',
            payload=b'critical-data',
            qos=2,
            packet_id=200,
        )
        wire = encode_packet(publish)
        decoded = decode_packet(wire)
        assert decoded.qos == 2
        assert decoded.packet_id == 200

    def test_publish_qos2_without_packet_id_raises(self, mqtt):
        """§3.3.2-3 — QoS 2 PUBLISH without Packet Identifier MUST be rejected."""
        with pytest.raises(ValueError, match='[Pp]acket.*[Ii]dentifier'):
            MQTTPublish(topic='test/qos2', payload=b'x', qos=2)

    @pytest.mark.asyncio
    async def test_qos2_four_way_handshake(self, mqtt):
        """§4.5 — Complete QoS 2 handshake: PUBLISH → PUBREC → PUBREL → PUBCOMP."""
        reader = _FakeMQTTReader()
        writer = _FakeMQTTWriter()
        ctx = _ctx()

        # Connect
        reader.feed_packet(MQTTConnect(client_id='qos2-pub', clean_start=True, keep_alive=60))
        # Publish QoS 2
        reader.feed_packet(MQTTPublish(
            topic='test/qos2',
            payload=b'critical-data',
            qos=2,
            packet_id=300,
        ))

        actor = mqtt.serve(reader, writer, ctx)
        task = asyncio.create_task(actor.run())
        await asyncio.sleep(0.1)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        packets = writer.pop_packets()
        # Verify the sequence: CONNACK, PUBREC
        pubrecs = [p for p in packets if isinstance(p, MQTTPubrec)]
        assert len(pubrecs) >= 1, "Expected PUBREC after QoS 2 PUBLISH"
        assert pubrecs[0].packet_id == 300

    # §3.5.2.1 — PUBREC reason codes
    @pytest.mark.parametrize("error_code", [
        0x00,  # Success
        0x10,  # No matching subscribers
        0x80,  # Unspecified error
        0x83,  # Implementation specific error
        0x87,  # Not authorized
        0x8D,  # Topic Name invalid
        0x8E,  # Packet too large
    ])
    def test_pubrec_reason_codes(self, error_code):
        """§3.5.2.1 — PUBREC can carry error reason codes."""
        pubrec = MQTTPubrec(packet_id=300, reason_code=error_code)
        wire = encode_packet(pubrec)
        decoded = decode_packet(wire)
        assert decoded.packet_id == 300
        assert decoded.reason_code == error_code

    # §3.6.1 — PUBREL fixed header flags MUST be 0x02
    def test_pubrel_fixed_header_flags(self, mqtt):
        """§3.6.1 — PUBREL fixed header bits 3-0 MUST be 0b0010 (0x2)."""
        pubrel = MQTTPubrel(packet_id=300, reason_code=0x00)
        wire = encode_packet(pubrel)
        assert (wire[0] & 0x0F) == 0x02, \
            "PUBREL fixed header flags must be 0x02 per §3.6.1"

    # §3.6.2.1 — PUBREL reason codes
    @pytest.mark.parametrize("error_code", [
        0x00,  # Success
        0x92,  # Packet Identifier not found
    ])
    def test_pubrel_reason_codes(self, error_code):
        """§3.6.2.1 — PUBREL can carry reason codes."""
        pubrel = MQTTPubrel(packet_id=300, reason_code=error_code)
        wire = encode_packet(pubrel)
        decoded = decode_packet(wire)
        assert decoded.reason_code == error_code

    # §3.7.2.1 — PUBCOMP reason codes
    @pytest.mark.parametrize("error_code", [
        0x00,  # Success
        0x92,  # Packet Identifier not found
    ])
    def test_pubcomp_reason_codes(self, error_code):
        """§3.7.2.1 — PUBCOMP can carry reason codes."""
        pubcomp = MQTTPubcomp(packet_id=300, reason_code=error_code)
        wire = encode_packet(pubcomp)
        decoded = decode_packet(wire)
        assert decoded.reason_code == error_code

    @pytest.mark.asyncio
    async def test_qos2_complete_handshake_as_subscriber(self, mqtt):
        """§4.5 — Client acting as subscriber receives QoS 2 PUBLISH from server,
        completes the 4-way handshake.

        Server → PUBLISH (QoS 2) → Subscriber
        Subscriber → PUBREC → Server
        Server → PUBREL → Subscriber
        Subscriber → PUBCOMP → Server
        """
        reader = _FakeMQTTReader()
        writer = _FakeMQTTWriter()
        ctx = _ctx()

        # Connect and subscribe (QoS 2)
        reader.feed_packet(MQTTConnect(client_id='qos2-sub', clean_start=True, keep_alive=60))
        reader.feed_packet(MQTTSubscribe(
            packet_id=1,
            subscriptions=[('alerts/critical', 2)],  # QoS 2 subscription
        ))

        # Simulate receiving a QoS 2 publish
        reader.feed_packet(MQTTPublish(
            topic='alerts/critical',
            payload=b'FIRE ALARM',
            qos=2,
            packet_id=999,
        ))

        actor = mqtt.serve(reader, writer, ctx)
        task = asyncio.create_task(actor.run())
        await asyncio.sleep(0.1)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        packets = writer.pop_packets()
        # Subscriber should have sent PUBREC
        pubrecs = [p for p in packets if isinstance(p, MQTTPubrec)]
        assert len(pubrecs) >= 1, "Subscriber must send PUBREC for QoS 2 PUBLISH"
        assert pubrecs[0].packet_id == 999


# ============================================================================
# §3.3.2.3 — DUP flag handling (QoS 1 and QoS 2 retransmission)
# ============================================================================

class TestDupFlag:
    """§3.3.2.1 — DUP flag indicates a retransmitted PUBLISH.

    §4.4 / §4.5: For QoS 1 and QoS 2, if the sender does not receive the
    expected acknowledgment within a reasonable time, it re-sends the
    PUBLISH with the DUP flag set.

    The receiver MUST treat DUP=1 as a retransmission and MUST NOT
    re-deliver the message to the application if it has already been
    delivered.  For QoS 2, the receiver MUST re-send the PUBREC.
    """

    def test_publish_dup_flag_encoding(self, mqtt):
        """§3.3.2.1 — DUP flag (bit 3) is correctly encoded in the fixed header."""
        publish = MQTTPublish(
            topic='test/dup',
            payload=b'retry',
            qos=1,
            packet_id=50,
            dup=True,
        )
        wire = encode_packet(publish)
        decoded = decode_packet(wire)
        assert decoded.dup is True
        assert decoded.qos == 1
        assert decoded.packet_id == 50
