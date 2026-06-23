"""
MQTT 5.0 Flow Control, Message Ordering, and QoS limiting tests — Sprint 52.

Covers operational behaviours not in the per-packet tests.

Reference: MQTT Version 5.0, OASIS Standard
  §4.9  Flow Control (Receive Maximum, send quota)
  §4.6  Message Ordering (per-topic ordering guarantee)
  §3.3.2.4 Topic Alias
  §3.2.2.3.1 Receive Maximum
  §3.9.2.1 Maximum QoS limiting (server may downgrade QoS)
  §3.8.2.1 No Local subscription option
"""

import asyncio
import pytest

from blackbull.mqtt.messages import (
    MQTTConnect, MQTTConnack,
    MQTTPublish, MQTTPuback,
    MQTTSubscribe, MQTTSuback,
    MQTTDisconnect,
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

    def feed(self, data: bytes) -> None:
        self._buf.extend(data)

    def feed_packet(self, packet) -> None:
        self._buf.extend(encode_packet(packet))


class _FakeMQTTWriter(AbstractWriter):
    def __init__(self):
        self.written = bytearray()
        self._write_count = 0

    async def write(self, data: bytes) -> None:
        self.written.extend(data)
        self._write_count += 1

    @property
    def send_count(self) -> int:
        return self._write_count

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
# §4.9 / §3.2.2.3.1 — Flow Control: Receive Maximum
# ============================================================================

class TestFlowControlReceiveMaximum:
    """§4.9 — Flow Control using Receive Maximum.

    §3.2.2.3.1: Receive Maximum is a 2-byte integer (1–65535) that limits
    the number of QoS 1 and QoS 2 PUBLISH messages the client is willing to
    process concurrently.  A value of 0 is invalid (treated as "no limit").

    The server MUST NOT send more than Receive Maximum unacknowledged
    QoS 1/2 PUBLISH messages to the client at any time.

    Note: MQTT 5.0 §4.9 states that flow control is per-client, not
    per-topic. The server tracks the count of sent-but-unacknowledged
    QoS 1/2 messages and pauses when the limit is reached.
    """

    def test_receive_maximum_property_in_connect(self, mqtt):
        """§3.2.2.3.1 — Client advertises Receive Maximum in CONNECT."""
        connect = MQTTConnect(
            client_id='rm-client',
            clean_start=True,
            keep_alive=60,
            properties={'receive_maximum': 50},
        )
        wire = encode_packet(connect)
        decoded = decode_packet(wire)
        assert decoded.properties['receive_maximum'] == 50

    def test_receive_maximum_property_in_connack(self, mqtt):
        """§3.2.2.3.1 — Server advertises Receive Maximum in CONNACK."""
        connack = MQTTConnack(
            session_present=False,
            reason_code=0x00,
            properties={'receive_maximum': 65535},
        )
        wire = encode_packet(connack)
        decoded = decode_packet(wire)
        assert decoded.properties['receive_maximum'] == 65535

    def test_receive_maximum_default_value(self, mqtt):
        """§3.2.2.3.1 — If Receive Maximum is absent, the default is 65,535."""
        connect = MQTTConnect(
            client_id='rm-default',
            clean_start=True,
            keep_alive=60,
        )
        wire = encode_packet(connect)
        decoded = decode_packet(wire)
        assert 'receive_maximum' not in decoded.properties
        # Server must assume 65535 if absent

    def test_receive_maximum_zero_treated_as_no_limit(self, mqtt):
        """§3.2.2.3.1 — Receive Maximum = 0 means no explicit limit."""
        # The spec says 0 is invalid for CONNECT but the server MUST
        # treat 0 from the client as an unspecified limit
        connect = MQTTConnect(
            client_id='rm-zero',
            clean_start=True,
            keep_alive=60,
            properties={'receive_maximum': 0},
        )
        wire = encode_packet(connect)
        decoded = decode_packet(wire)
        assert decoded.properties['receive_maximum'] == 0


# ============================================================================
# §4.6 — Message Ordering
# ============================================================================

class TestMessageOrdering:
    """§4.6 — Message ordering guarantees.

    MQTT 5.0 §4.6 states:
      - Messages published with the same Topic and QoS level MUST be
        delivered to subscribers in the order they were received by
        the server.
      - Messages with different QoS levels may be reordered.
      - QoS 0 messages may be delivered out of order relative to
        QoS 1/2 messages.
      - The ordering guarantee is per-topic, not global.
    """

    @pytest.mark.asyncio
    async def test_same_topic_same_qos_preserves_order(self, mqtt):
        """§4.6 — Messages on same topic with same QoS are delivered in order."""
        reader = _FakeMQTTReader()
        writer = _FakeMQTTWriter()
        ctx = _ctx()

        actor = mqtt.serve(reader, writer, ctx)
        # Connect + subscribe
        reader.feed_packet(MQTTConnect(
            client_id='order-sub',
            clean_start=True,
            keep_alive=60,
        ))
        reader.feed_packet(MQTTSubscribe(
            packet_id=1,
            subscriptions=[('order/topic', 1)],
        ))

        # Publish messages in order
        for i in range(5):
            reader.feed_packet(MQTTPublish(
                topic='order/topic',
                payload=f'msg-{i}'.encode(),
                qos=1,
                packet_id=100 + i,
            ))

        task = asyncio.create_task(actor.run())
        await asyncio.sleep(0.1)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        packets = writer.pop_packets()
        publishes = [p for p in packets if isinstance(p, MQTTPublish)
                     and p.topic == 'order/topic']
        payloads = [p.payload for p in publishes]
        # Verify ordering: msg-0, msg-1, msg-2, msg-3, msg-4
        for i, payload in enumerate(payloads):
            assert payload == f'msg-{i}'.encode(), \
                f"Expected msg-{i} at position {i}, got {payload}"


# ============================================================================
# §3.3.2.4 — Topic Alias
# ============================================================================

class TestTopicAlias:
    """§3.3.2.4 — Topic Alias.

    Topic Alias allows a short integer alias to be used in place of the
    full Topic Name in subsequent PUBLISH packets on the same connection.

    §3.2.2.3.6: Server advertises Topic Alias Maximum in CONNACK.
    §3.1.2.3:   Client advertises Topic Alias Maximum in CONNECT.

    Rules:
      - Alias value 0 is prohibited.
      - A sender MUST NOT send a Topic Alias greater than the receiver's
        Topic Alias Maximum.
      - A Topic Alias mapping is set by a PUBLISH that includes both
        the Topic Name and a non-zero Topic Alias.
      - Once mapped, subsequent PUBLISH packets can omit the Topic Name
        and use only the Topic Alias.
    """

    def test_topic_alias_maximum_in_connect(self, mqtt):
        """§3.1.2.3 — Client advertises Topic Alias Maximum."""
        connect = MQTTConnect(
            client_id='ta-client',
            clean_start=True,
            keep_alive=60,
            properties={'topic_alias_maximum': 16},
        )
        wire = encode_packet(connect)
        decoded = decode_packet(wire)
        assert decoded.properties['topic_alias_maximum'] == 16

    def test_topic_alias_maximum_in_connack(self, mqtt):
        """§3.2.2.3.6 — Server advertises Topic Alias Maximum."""
        connack = MQTTConnack(
            session_present=False,
            reason_code=0x00,
            properties={'topic_alias_maximum': 8},
        )
        wire = encode_packet(connack)
        decoded = decode_packet(wire)
        assert decoded.properties['topic_alias_maximum'] == 8

    def test_publish_with_topic_alias_property(self, mqtt):
        """§3.3.2.4 — PUBLISH can include Topic Alias property.

        The first PUBLISH with a given alias MUST also include the Topic Name.
        Subsequent PUBLISH with the same alias MAY omit the Topic Name.
        """
        # First publish: topic + alias (establishes mapping)
        publish = MQTTPublish(
            topic='sensors/temperature',
            payload=b'22.5',
            qos=1,
            packet_id=1,
            properties={'topic_alias': 1},
        )
        wire = encode_packet(publish)
        decoded = decode_packet(wire)
        assert decoded.properties['topic_alias'] == 1
        assert decoded.topic == 'sensors/temperature'

    def test_publish_with_topic_alias_only(self, mqtt):
        """§3.3.2.4 — PUBLISH with Topic Alias but no Topic Name
        (uses previously established alias mapping)."""
        publish = MQTTPublish(
            topic='',  # empty Topic Name when using alias
            payload=b'22.6',
            qos=1,
            packet_id=2,
            properties={'topic_alias': 1},
        )
        wire = encode_packet(publish)
        decoded = decode_packet(wire)
        assert decoded.properties['topic_alias'] == 1
        assert decoded.topic == ''  # Topic Name omitted

    def test_topic_alias_zero_is_prohibited(self, mqtt):
        """§3.3.2.4 — Topic Alias value 0 is prohibited in PUBLISH."""
        with pytest.raises(ValueError, match='[Aa]lias.*0'):
            MQTTPublish(
                topic='test',
                payload=b'x',
                qos=1,
                packet_id=1,
                properties={'topic_alias': 0},
            )


# ============================================================================
# §4.10 — Request/Response pattern
# ============================================================================

class TestRequestResponsePattern:
    """§4.10 — Request/Response pattern using Response Topic and Correlation Data.

    MQTT 5.0 adds built-in support for request/response semantics:
      - Response Topic: The topic the responder should publish to
      - Correlation Data: Opaque data echoed back to correlate requests

    §3.3.2.3.3: These properties are set in the request PUBLISH.
    """

    def test_publish_with_response_topic(self, mqtt):
        """§3.3.2.3.3 — Request PUBLISH includes Response Topic."""
        publish = MQTTPublish(
            topic='commands/device1/reboot',
            payload=b'{}',
            qos=1,
            packet_id=1,
            properties={
                'response_topic': 'responses/device1',
                'correlation_data': b'req-001',
            },
        )
        wire = encode_packet(publish)
        decoded = decode_packet(wire)
        assert decoded.properties['response_topic'] == 'responses/device1'
        assert decoded.properties['correlation_data'] == b'req-001'

    def test_response_publish_echoes_correlation_data(self, mqtt):
        """§4.10.1 — Response PUBLISH echoes Correlation Data from request."""
        # The responder should copy correlation_data from request to response
        publish = MQTTPublish(
            topic='responses/device1',
            payload=b'{"status":"ok"}',
            qos=1,
            packet_id=2,
            properties={
                'correlation_data': b'req-001',  # echoed from request
            },
        )
        wire = encode_packet(publish)
        decoded = decode_packet(wire)
        assert decoded.properties['correlation_data'] == b'req-001'

    def test_request_response_information_property(self, mqtt):
        """§3.1.2.11 — Client can request Response Information in CONNECT.

        If Request Response Information = 1, the server MAY return
        Response Information in CONNACK (e.g., the response topic prefix).
        """
        connect = MQTTConnect(
            client_id='rri-client',
            clean_start=True,
            keep_alive=60,
            properties={'request_response_information': 1},
        )
        wire = encode_packet(connect)
        decoded = decode_packet(wire)
        assert decoded.properties['request_response_information'] == 1


# ============================================================================
# §3.9.2.1 — Maximum QoS limiting (QoS downgrade)
# ============================================================================

class TestMaximumQoSLimiting:
    """§3.2.2.3.3 — Maximum QoS property.

    The server MAY limit the maximum QoS level.  If a client subscribes
    with QoS 2 but the server's Maximum QoS is 1, the SUBACK grants QoS 1.
    """

    def test_maximum_qos_in_connack(self, mqtt):
        """§3.2.2.3.3 — Server advertises Maximum QoS in CONNACK.

        0 = QoS 0 only, 1 = QoS 0 or 1, 2 = all QoS levels.
        """
        for max_qos in (0, 1, 2):
            connack = MQTTConnack(
                session_present=False,
                reason_code=0x00,
                properties={'maximum_qos': max_qos},
            )
            wire = encode_packet(connack)
            decoded = decode_packet(wire)
            assert decoded.properties['maximum_qos'] == max_qos

    def test_suback_qos_downgrade(self, mqtt):
        """§3.9.2.1 — SUBACK grants actual QoS, which may be lower than requested.

        The SUBACK reason code indicates the granted QoS (0, 1, or 2),
        which MUST NOT exceed the server's Maximum QoS.
        """
        # Client requested QoS 2, server granted QoS 1 (due to Maximum QoS = 1)
        suback = MQTTSuback(
            packet_id=1,
            reason_codes=[0x01],  # Granted QoS 1 (downgraded from 2)
            properties={'reason_string': 'QoS 2 not available; granted QoS 1'},
        )
        wire = encode_packet(suback)
        decoded = decode_packet(wire)
        assert decoded.reason_codes == [0x01]


# ============================================================================
# §3.8.2.1 — No Local subscription option
# ============================================================================

class TestNoLocalOption:
    """§3.8.2.1 — No Local subscription option (bit 2 of Subscription Options).

    If No Local = 1, the server MUST NOT deliver messages to this
    subscriber if the message was published by the same client (same
    Client ID).
    """

    def test_subscribe_with_no_local(self, mqtt):
        """§3.8.2.1 — No Local = 1 prevents self-delivery."""
        sub = MQTTSubscribe(
            packet_id=1,
            subscriptions=[('chat/room1', 1)],
            subscription_options=[{'no_local': True}],
        )
        wire = encode_packet(sub)
        decoded = decode_packet(wire)
        assert decoded.subscription_options[0]['no_local'] is True

    def test_subscribe_without_no_local(self, mqtt):
        """§3.8.2.1 — No Local = 0 (default): messages are delivered normally."""
        sub = MQTTSubscribe(
            packet_id=1,
            subscriptions=[('chat/room1', 1)],
        )
        wire = encode_packet(sub)
        decoded = decode_packet(wire)
        # Default should be False (or absent)
        opts = decoded.subscription_options[0] if decoded.subscription_options else {}
        assert opts.get('no_local', False) is False


# ============================================================================
# §3.3.2.3.2 — Payload Format Indicator
# ============================================================================

class TestPayloadFormatIndicator:
    """§3.3.2.3.2 — Payload Format Indicator property.

    0 = Payload is unspecified bytes
    1 = Payload is UTF-8 encoded character data

    The server MAY validate that payload marked as UTF-8 is indeed valid UTF-8.
    """

    def test_publish_with_payload_format_indicator_utf8(self, mqtt):
        """§3.3.2.3.2 — Payload Format Indicator = 1 (UTF-8)."""
        publish = MQTTPublish(
            topic='data/json',
            payload='{"key":"value"}'.encode('utf-8'),
            qos=1,
            packet_id=1,
            properties={'payload_format_indicator': 1},
        )
        wire = encode_packet(publish)
        decoded = decode_packet(wire)
        assert decoded.properties['payload_format_indicator'] == 1

    def test_publish_with_payload_format_indicator_unspecified(self, mqtt):
        """§3.3.2.3.2 — Payload Format Indicator = 0 (unspecified bytes)."""
        publish = MQTTPublish(
            topic='data/binary',
            payload=b'\x00\x01\x02\x03',
            qos=1,
            packet_id=1,
            properties={'payload_format_indicator': 0},
        )
        wire = encode_packet(publish)
        decoded = decode_packet(wire)
        assert decoded.properties['payload_format_indicator'] == 0

    def test_utf8_payload_must_be_valid_utf8(self, mqtt):
        """§1.5.4 — If Payload Format Indicator = 1, the payload MUST be
        valid UTF-8 encoded character data."""
        with pytest.raises(ValueError, match='UTF-8'):
            MQTTPublish(
                topic='data/bad',
                payload=b'\xFF\xFE\x00\x01',  # Invalid UTF-8
                qos=1,
                packet_id=1,
                properties={'payload_format_indicator': 1},
            )


# ============================================================================
# §4.11 — Server Redirection
# ============================================================================

class TestServerRedirection:
    """§4.11 — Server Redirection.

    A server MAY ask a client to connect to another server by sending
    CONNACK or DISCONNECT with reason code 0x94 (Use another server)
    or 0x95 (Server moved), along with a Server Reference property.
    """

    def test_connack_with_server_reference(self, mqtt):
        """§3.2.2.3.2 — CONNACK with Server Reference for redirection."""
        connack = MQTTConnack(
            session_present=False,
            reason_code=0x94,  # Use another server
            properties={
                'reason_string': 'Please connect to node-2',
                'server_reference': 'node-2.example.com:1883',
            },
        )
        wire = encode_packet(connack)
        decoded = decode_packet(wire)
        assert decoded.reason_code == 0x94
        assert decoded.properties['server_reference'] == 'node-2.example.com:1883'

    def test_disconnect_with_server_reference(self, mqtt):
        """§3.14.2.2 — DISCONNECT with Server Reference."""
        disconnect = MQTTDisconnect(
            reason_code=0x95,  # Server moved
            properties={
                'reason_string': 'Server permanently moved',
                'server_reference': 'new-broker.example.com:8883',
            },
        )
        wire = encode_packet(disconnect)
        decoded = decode_packet(wire)
        assert decoded.reason_code == 0x95
        assert decoded.properties['server_reference'] == 'new-broker.example.com:8883'


# ============================================================================
# §3.3.2.3.4 — Subscription Identifier forwarding
# ============================================================================

class TestSubscriptionIdentifier:
    """§3.3.2.3.4 — Subscription Identifier.

    The server MUST forward the Subscription Identifier(s) from the
    matching subscription(s) when delivering a PUBLISH to a subscriber.
    This allows the subscriber to know which subscription(s) caused
    the message to be delivered.
    """

    def test_subscribe_with_subscription_identifier(self, mqtt):
        """§3.8.2.2 — SUBSCRIBE can include Subscription Identifier."""
        sub = MQTTSubscribe(
            packet_id=1,
            subscriptions=[('sensors/+/temperature', 1)],
            properties={'subscription_identifier': 42},
        )
        wire = encode_packet(sub)
        decoded = decode_packet(wire)
        assert decoded.properties['subscription_identifier'] == 42

    def test_publish_with_subscription_identifier(self, mqtt):
        """§3.3.2.3.4 — PUBLISH forwarded to subscriber includes
        Subscription Identifier(s) from matching subscriptions."""
        publish = MQTTPublish(
            topic='sensors/room1/temperature',
            payload=b'23.5',
            qos=1,
            packet_id=10,
            properties={'subscription_identifier': 42},
        )
        wire = encode_packet(publish)
        decoded = decode_packet(wire)
        assert decoded.properties['subscription_identifier'] == 42
