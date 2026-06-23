"""
MQTT 5.0 SUBSCRIBE / SUBACK / UNSUBSCRIBE / UNSUBACK conformance tests — Sprint 52.

Verifies subscription management against the MQTT 5.0 OASIS Standard.

Reference: MQTT Version 5.0, OASIS Standard
  §3.8  SUBSCRIBE    – Subscribe Request
  §3.9  SUBACK       – Subscribe Acknowledgment
  §3.10 UNSUBSCRIBE  – Unsubscribe Request
  §3.11 UNSUBACK     – Unsubscribe Acknowledgment
  §4.7  Topic Names and Topic Filters (§4.7.1 Topic wildcards)
"""

import pytest

from blackbull.mqtt.messages import (
    MQTTSubscribe, MQTTSuback,
    MQTTUnsubscribe, MQTTUnsuback,
    encode_packet, decode_packet,
)


# ============================================================================
# §3.8 — SUBSCRIBE packet
# ============================================================================

class TestSubscribePacket:
    """§3.8 — SUBSCRIBE packet structure and validation.

    A SUBSCRIBE packet is sent from the client to the server to create one or
    more subscriptions.  Each subscription pairs a Topic Filter with a
    maximum QoS level.

    §3.8.1: SUBSCRIBE fixed header bits 3-0 MUST be 0b0010 (value 2).
    """

    def test_subscribe_fixed_header_flags(self):
        """§3.8.1 — SUBSCRIBE fixed header flags MUST be 0x02."""
        sub = MQTTSubscribe(
            packet_id=1,
            subscriptions=[('test/topic', 0)],
        )
        wire = encode_packet(sub)
        assert (wire[0] & 0x0F) == 0x02, \
            "SUBSCRIBE fixed header flags must be 0x02 per §3.8.1"

    def test_subscribe_single_topic(self):
        """§3.8.2 — SUBSCRIBE with a single Topic Filter."""
        sub = MQTTSubscribe(
            packet_id=1,
            subscriptions=[('sensors/temperature', 1)],
        )
        wire = encode_packet(sub)
        decoded = decode_packet(wire)
        assert decoded.packet_id == 1
        assert len(decoded.subscriptions) == 1
        assert decoded.subscriptions[0] == ('sensors/temperature', 1)

    def test_subscribe_multiple_topics(self):
        """§3.8.2 — SUBSCRIBE with multiple Topic Filters in one packet."""
        sub = MQTTSubscribe(
            packet_id=5,
            subscriptions=[
                ('sensors/+/temperature', 1),
                ('sensors/#', 0),
                ('alerts/critical', 2),
            ],
        )
        wire = encode_packet(sub)
        decoded = decode_packet(wire)
        assert decoded.packet_id == 5
        assert len(decoded.subscriptions) == 3

    def test_subscribe_with_qos_0_1_2(self):
        """§3.8.2.1 — Subscription Options: QoS 0, 1, or 2 requested."""
        for qos in (0, 1, 2):
            sub = MQTTSubscribe(
                packet_id=100,
                subscriptions=[(f'qos{qos}/topic', qos)],
            )
            wire = encode_packet(sub)
            decoded = decode_packet(wire)
            assert decoded.subscriptions[0][1] == qos

    def test_subscribe_with_no_local_option(self):
        """§3.8.2.1 — Subscription Options: No Local (bit 2).

        No Local = 1 means the server MUST NOT forward messages published
        by the subscribing client itself.
        """
        sub = MQTTSubscribe(
            packet_id=1,
            subscriptions=[('test/topic', 1)],
            subscription_options=[{'no_local': True}],
        )
        wire = encode_packet(sub)
        decoded = decode_packet(wire)
        assert decoded.subscription_options[0]['no_local'] is True

    def test_subscribe_with_retain_as_published(self):
        """§3.8.2.1 — Subscription Options: Retain As Published (bit 3).

        Retain As Published = 1 means the server forwards retained messages
        with the original RETAIN flag value.
        """
        sub = MQTTSubscribe(
            packet_id=1,
            subscriptions=[('test/topic', 1)],
            subscription_options=[{'retain_as_published': True}],
        )
        wire = encode_packet(sub)
        decoded = decode_packet(wire)
        assert decoded.subscription_options[0]['retain_as_published'] is True

    def test_subscribe_with_retain_handling(self):
        """§3.8.2.1 — Subscription Options: Retain Handling (bits 5-4).

        - 0 = Send retained messages at subscribe time
        - 1 = Send retained messages only if subscription does not exist
        - 2 = Do not send retained messages at subscribe time
        """
        for rh in (0, 1, 2):
            sub = MQTTSubscribe(
                packet_id=1,
                subscriptions=[('test/topic', 1)],
                subscription_options=[{'retain_handling': rh}],
            )
            wire = encode_packet(sub)
            decoded = decode_packet(wire)
            assert decoded.subscription_options[0]['retain_handling'] == rh

    def test_subscribe_without_packet_id_raises(self):
        """§3.8.2 — SUBSCRIBE MUST have a Packet Identifier."""
        with pytest.raises(ValueError, match='[Pp]acket.*[Ii]dentifier'):
            MQTTSubscribe(subscriptions=[('test/topic', 0)])


# ============================================================================
# §3.8.3 — SUBSCRIBE Payload: Topic Filter validation
# ============================================================================

class TestSubscribeTopicFilters:
    """§3.8.3 — Topic Filter rules in SUBSCRIBE payload.

    Topic Filters can include wildcards (§4.7.1):
      - '+'  (single-level wildcard): matches exactly one topic level
      - '#'  (multi-level wildcard): matches any number of levels (must be last)
    """

    def test_subscribe_with_single_level_wildcard(self):
        """§4.7.1.2 — Single-level wildcard '+' matches one level."""
        sub = MQTTSubscribe(
            packet_id=1,
            subscriptions=[('sensors/+/temperature', 1)],
        )
        wire = encode_packet(sub)
        decoded = decode_packet(wire)
        assert decoded.subscriptions[0][0] == 'sensors/+/temperature'

    def test_subscribe_with_multi_level_wildcard(self):
        """§4.7.1.3 — Multi-level wildcard '#' matches any number of levels."""
        sub = MQTTSubscribe(
            packet_id=1,
            subscriptions=[('sensors/#', 1)],
        )
        wire = encode_packet(sub)
        decoded = decode_packet(wire)
        assert decoded.subscriptions[0][0] == 'sensors/#'

    def test_subscribe_with_shared_subscription(self):
        """§4.8 — Shared Subscriptions: $share/{ShareName}/{TopicFilter}.

        Shared subscriptions allow multiple clients to share the same
        subscription; only one client receives each message.
        """
        sub = MQTTSubscribe(
            packet_id=1,
            subscriptions=[('$share/group1/sensors/temperature', 1)],
        )
        wire = encode_packet(sub)
        decoded = decode_packet(wire)
        assert decoded.subscriptions[0][0] == '$share/group1/sensors/temperature'


# ============================================================================
# §3.9 — SUBACK packet
# ============================================================================

class TestSubackPacket:
    """§3.9 — SUBACK packet structure.

    A SUBACK is sent by the server in response to a SUBSCRIBE.  It contains
    one Reason Code per Topic Filter in the SUBSCRIBE, in the same order.

    §3.9.2.1 — SUBACK Reason Codes include both success (QoS levels) and
    error codes.
    """

    def test_suback_with_granted_qos(self):
        """§3.9.2.1 — SUBACK grants a maximum QoS for each subscription."""
        suback = MQTTSuback(
            packet_id=1,
            reason_codes=[0, 1, 2],  # Granted QoS 0, 1, 2
        )
        wire = encode_packet(suback)
        decoded = decode_packet(wire)
        assert decoded.packet_id == 1
        assert decoded.reason_codes == [0, 1, 2]

    @pytest.mark.parametrize("reason_code,meaning", [
        (0x00, 'Granted QoS 0'),
        (0x01, 'Granted QoS 1'),
        (0x02, 'Granted QoS 2'),
        (0x80, 'Unspecified error'),
        (0x83, 'Implementation specific error'),
        (0x87, 'Not authorized'),
        (0x8F, 'Quota exceeded'),
        (0x90, 'Topic Filter invalid'),
        (0x91, 'Payload format invalid'),  # §3.9.2.1 — doesn't apply but code is reserved
        (0x97, 'Shared Subscriptions not supported'),
        (0x98, 'Subscription Identifiers not supported'),
        (0x99, 'Wildcard Subscriptions not supported'),
    ])
    def test_suback_reason_codes(self, reason_code, meaning):
        """§3.9.2.1 — All valid SUBACK reason codes are encodable."""
        suback = MQTTSuback(
            packet_id=42,
            reason_codes=[reason_code],
        )
        wire = encode_packet(suback)
        decoded = decode_packet(wire)
        assert decoded.reason_codes == [reason_code]

    def test_suback_with_properties(self):
        """§3.9.2.2 — SUBACK may include Reason String and User Properties."""
        suback = MQTTSuback(
            packet_id=1,
            reason_codes=[2, 0x80],
            properties={
                'reason_string': 'QoS 2 granted; second subscription failed',
                'user_properties': [('broker', 'blackbull')],
            },
        )
        wire = encode_packet(suback)
        decoded = decode_packet(wire)
        assert decoded.reason_codes == [2, 0x80]
        assert decoded.properties['reason_string'] == (
            'QoS 2 granted; second subscription failed'
        )


# ============================================================================
# §3.10 — UNSUBSCRIBE packet
# ============================================================================

class TestUnsubscribePacket:
    """§3.10 — UNSUBSCRIBE packet structure.

    An UNSUBSCRIBE is sent from the client to the server to unsubscribe
    from one or more topics.

    §3.10.1: UNSUBSCRIBE fixed header bits 3-0 MUST be 0b0010 (value 2).
    """

    def test_unsubscribe_fixed_header_flags(self):
        """§3.10.1 — UNSUBSCRIBE fixed header flags MUST be 0x02."""
        unsub = MQTTUnsubscribe(
            packet_id=1,
            topics=['test/topic'],
        )
        wire = encode_packet(unsub)
        assert (wire[0] & 0x0F) == 0x02, \
            "UNSUBSCRIBE fixed header flags must be 0x02 per §3.10.1"

    def test_unsubscribe_single_topic(self):
        """§3.10.2 — UNSUBSCRIBE with a single Topic Filter."""
        unsub = MQTTUnsubscribe(
            packet_id=2,
            topics=['sensors/+/temperature'],
        )
        wire = encode_packet(unsub)
        decoded = decode_packet(wire)
        assert decoded.packet_id == 2
        assert decoded.topics == ['sensors/+/temperature']

    def test_unsubscribe_multiple_topics(self):
        """§3.10.2 — UNSUBSCRIBE with multiple Topic Filters."""
        unsub = MQTTUnsubscribe(
            packet_id=10,
            topics=['sensors/#', 'alerts/critical', 'status/heartbeat'],
        )
        wire = encode_packet(unsub)
        decoded = decode_packet(wire)
        assert decoded.packet_id == 10
        assert len(decoded.topics) == 3

    def test_unsubscribe_without_packet_id_raises(self):
        """§3.10.2 — UNSUBSCRIBE MUST have a Packet Identifier."""
        with pytest.raises(ValueError, match='[Pp]acket.*[Ii]dentifier'):
            MQTTUnsubscribe(topics=['test/topic'])


# ============================================================================
# §3.11 — UNSUBACK packet
# ============================================================================

class TestUnsubackPacket:
    """§3.11 — UNSUBACK packet structure.

    UNSUBACK is sent by the server in response to UNSUBSCRIBE.  It contains
    one Reason Code per Topic Filter in the UNSUBSCRIBE.

    §3.11.2.1 — UNSUBACK Reason Codes.
    """

    def test_unsuback_success(self):
        """§3.11.2.1 — UNSUBACK Success (0x00)."""
        unsuback = MQTTUnsuback(
            packet_id=10,
            reason_codes=[0x00, 0x00],
        )
        wire = encode_packet(unsuback)
        decoded = decode_packet(wire)
        assert decoded.packet_id == 10
        assert decoded.reason_codes == [0x00, 0x00]

    @pytest.mark.parametrize("reason_code", [
        0x00,  # Success
        0x11,  # No subscription existed
        0x80,  # Unspecified error
        0x83,  # Implementation specific error
        0x87,  # Not authorized
        0x90,  # Topic Filter invalid
    ])
    def test_unsuback_reason_codes(self, reason_code):
        """§3.11.2.1 — All valid UNSUBACK reason codes."""
        unsuback = MQTTUnsuback(
            packet_id=1,
            reason_codes=[reason_code],
        )
        wire = encode_packet(unsuback)
        decoded = decode_packet(wire)
        assert decoded.reason_codes == [reason_code]

    def test_unsuback_with_properties(self):
        """§3.11.2.2 — UNSUBACK may include Reason String."""
        unsuback = MQTTUnsuback(
            packet_id=5,
            reason_codes=[0x00, 0x11],
            properties={'reason_string': 'First OK, second did not exist'},
        )
        wire = encode_packet(unsuback)
        decoded = decode_packet(wire)
        assert decoded.properties['reason_string'] == (
            'First OK, second did not exist'
        )
