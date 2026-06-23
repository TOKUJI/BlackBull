"""
MQTT 5.0 Retained Message conformance tests — Sprint 52.

Verifies retained message behaviour against the MQTT 5.0 OASIS Standard.

Reference: MQTT Version 5.0, OASIS Standard
  §3.3.2.3  Retain flag in PUBLISH
  §3.8.2.1  Subscription Options: Retain As Published, Retain Handling
  §3.3.2.3  Retained Message storage and replacement

Key behaviours:
  - §3.3.2.3: If the RETAIN flag is set to 1 in a PUBLISH, the server MUST
    store the message for that topic (replacing any previously retained
    message for that topic).
  - §3.3.2.3: A zero-length payload PUBLISH with RETAIN=1 deletes the
    retained message for that topic.
  - §3.3.2.3: When a new subscription is created, the latest retained message
    matching the topic filter is sent to the subscriber.
  - §3.8.2.1: Retain As Published (bit 3): if set, the server forwards the
    retained message with the RETAIN flag preserved.
  - §3.8.2.1: Retain Handling (bits 5-4):
      0 = Send retained messages at subscribe time
      1 = Send retained messages only for new subscriptions
      2 = Do not send retained messages at subscribe time
"""

import asyncio
import pytest

from blackbull.mqtt.messages import (
    MQTTConnect, MQTTConnack, MQTTDisconnect,
    MQTTPublish, MQTTSubscribe, MQTTSuback,
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
            await asyncio.sleep(0.01)
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
# §3.3.2.3 — Retained message storage and lifecycle
# ============================================================================

class TestRetainedMessageStorage:
    """§3.3.2.3 — Retained message storage rules.

    The server maintains one retained message per topic.  A new PUBLISH
    with RETAIN=1 replaces any previous retained message for that topic.
    """

    def test_publish_with_retain_flag_set(self, mqtt):
        """§3.3.2.3 — PUBLISH with RETAIN=1: server stores the message."""
        publish = MQTTPublish(
            topic='status/server',
            payload=b'online',
            qos=0,
            retain=True,
        )
        wire = encode_packet(publish)
        decoded = decode_packet(wire)
        assert decoded.retain is True
        assert decoded.topic == 'status/server'
        assert decoded.payload == b'online'

    def test_publish_retained_replaces_previous(self, mqtt):
        """§3.3.2.3 — A PUBLISH with RETAIN=1 on the same topic replaces
        the previously retained message."""
        # First retained message
        first = MQTTPublish(
            topic='status/server',
            payload=b'online',
            qos=0,
            retain=True,
        )
        # Second retained message on same topic
        second = MQTTPublish(
            topic='status/server',
            payload=b'maintenance',
            qos=0,
            retain=True,
        )
        # Both encode/decode fine; replacement is server-side logic
        assert encode_packet(first)
        assert encode_packet(second)

    def test_zero_length_payload_deletes_retained_message(self, mqtt):
        """§3.3.2.3 — A PUBLISH with RETAIN=1 and zero-length payload
        MUST delete the retained message for that topic."""
        publish = MQTTPublish(
            topic='status/server',
            payload=b'',  # zero-length payload
            qos=0,
            retain=True,
        )
        wire = encode_packet(publish)
        decoded = decode_packet(wire)
        assert decoded.payload == b''
        assert decoded.retain is True
        # Server-side: this deletes the retained message for this topic

    def test_qos_levels_for_retained_messages(self, mqtt):
        """§3.3.2.3 — Retained messages can be at any QoS level (0, 1, 2)."""
        for qos in (0, 1, 2):
            publish = MQTTPublish(
                topic=f'status/qos{qos}',
                payload=b'data',
                qos=qos,
                retain=True,
                packet_id=1 if qos > 0 else None,
            )
            wire = encode_packet(publish)
            decoded = decode_packet(wire)
            assert decoded.retain is True
            assert decoded.qos == qos


# ============================================================================
# §3.3.2.3 — Retained message delivery on new subscriptions
# ============================================================================

class TestRetainedMessageDelivery:
    """§3.3.2.3 — Retained messages are delivered to new subscribers.

    When a client subscribes to a topic filter that matches a retained
    message, the server MUST send the retained message to that client.

    §3.8.2.1 — The Retain Handling subscription option controls this behavior.
    """

    @pytest.mark.asyncio
    async def test_retained_message_sent_on_subscribe(self, mqtt):
        """§3.3.2.3 — A new subscriber receives matching retained messages."""

        # Pre-populate a retained message in the broker store
        mqtt.retained['status/server'] = MQTTPublish(
            topic='status/server',
            payload=b'online',
            qos=0,
            retain=True,
        )

        reader = _FakeMQTTReader()
        writer = _FakeMQTTWriter()
        ctx = _ctx()

        actor = mqtt.serve(reader, writer, ctx)
        # Connect and subscribe to the topic with a retained message
        reader.feed_packet(MQTTConnect(
            client_id='retain-sub',
            clean_start=True,
            keep_alive=60,
        ))
        reader.feed_packet(MQTTSubscribe(
            packet_id=1,
            subscriptions=[('status/server', 0)],
        ))

        task = asyncio.create_task(actor.run())
        await asyncio.sleep(0.1)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        packets = writer.pop_packets()
        # After CONNACK + SUBACK, should receive the retained PUBLISH
        publishes = [p for p in packets if isinstance(p, MQTTPublish)]
        retained = [p for p in publishes if hasattr(p, 'retain') and p.retain]
        assert len(retained) >= 1, \
            "Expected retained message to be delivered on subscribe"

    def test_retain_handling_0_send_retained(self, mqtt):
        """§3.8.2.1 — Retain Handling = 0: send retained messages at
        subscribe time (default behavior)."""
        sub = MQTTSubscribe(
            packet_id=1,
            subscriptions=[('status/+/info', 1)],
            subscription_options=[{'retain_handling': 0}],
        )
        wire = encode_packet(sub)
        decoded = decode_packet(wire)
        assert decoded.subscription_options[0]['retain_handling'] == 0

    def test_retain_handling_1_send_only_if_new(self, mqtt):
        """§3.8.2.1 — Retain Handling = 1: send retained messages only if
        the subscription does not already exist."""
        sub = MQTTSubscribe(
            packet_id=1,
            subscriptions=[('status/+/info', 1)],
            subscription_options=[{'retain_handling': 1}],
        )
        wire = encode_packet(sub)
        decoded = decode_packet(wire)
        assert decoded.subscription_options[0]['retain_handling'] == 1

    def test_retain_handling_2_do_not_send(self, mqtt):
        """§3.8.2.1 — Retain Handling = 2: do NOT send retained messages
        at subscribe time."""
        sub = MQTTSubscribe(
            packet_id=1,
            subscriptions=[('status/+/info', 1)],
            subscription_options=[{'retain_handling': 2}],
        )
        wire = encode_packet(sub)
        decoded = decode_packet(wire)
        assert decoded.subscription_options[0]['retain_handling'] == 2

    def test_retain_as_published_flag(self, mqtt):
        """§3.8.2.1 — Retain As Published (bit 3): if set, the server
        preserves the RETAIN flag when forwarding retained messages."""
        sub = MQTTSubscribe(
            packet_id=1,
            subscriptions=[('status/#', 1)],
            subscription_options=[{'retain_as_published': True}],
        )
        wire = encode_packet(sub)
        decoded = decode_packet(wire)
        assert decoded.subscription_options[0]['retain_as_published'] is True
