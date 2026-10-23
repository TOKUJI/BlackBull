"""
MQTT 5.0 message dataclass unit tests.

Tests individual MQTT message dataclasses in isolation (no I/O, no Actor).
Verifies that message objects behave correctly (construction, equality,
immutability, edge cases).

Reference: MQTT Version 5.0, OASIS Standard
"""

from typing import get_type_hints

import pytest

from blackbull.mqtt.messages import (
    # Control packet dataclasses
    MQTTConnect, MQTTPublish, MQTTSuback,
    MQTTPingreq, MQTTPingresp, MQTTDisconnect,
    # Enums
    MQTTPacketType, MQTTReasonCode, ProtocolLevel, ReasonCode,
    RESERVED_FLAGS_0010,
    # Codec
    encode_packet, decode_packet,
)


# ============================================================================
# MQTTPacketType enum
# ============================================================================

class TestPacketTypeEnum:
    """MQTTPacketType enum covers all 15 MQTT 5.0 packet types (§2.1.1 Table 2-1)."""

    def test_all_15_types_exist(self):
        """§2.1.1 Table 2-1 — Exactly 15 packet types are defined."""
        assert len(MQTTPacketType) == 15

    @pytest.mark.parametrize("ptype,name", [
        (1, 'CONNECT'), (2, 'CONNACK'), (3, 'PUBLISH'), (4, 'PUBACK'),
        (5, 'PUBREC'), (6, 'PUBREL'), (7, 'PUBCOMP'), (8, 'SUBSCRIBE'),
        (9, 'SUBACK'), (10, 'UNSUBSCRIBE'), (11, 'UNSUBACK'),
        (12, 'PINGREQ'), (13, 'PINGRESP'), (14, 'DISCONNECT'), (15, 'AUTH'),
    ])
    def test_the_packet_type_values_are_the_table_2_1_ones(self, ptype, name):
        assert MQTTPacketType(ptype).name == name

    def test_packet_type_from_raw_byte(self):
        """Extract type from first byte of fixed header (bits 7-4)."""
        connect = int(MQTTPacketType.CONNECT) << 4
        assert MQTTPacketType(connect >> 4) == MQTTPacketType.CONNECT
        pubrel = (int(MQTTPacketType.PUBREL) << 4) | RESERVED_FLAGS_0010
        assert MQTTPacketType((pubrel >> 4) & 0x0F) == MQTTPacketType.PUBREL


# ============================================================================
# MQTTReasonCode enum
# ============================================================================

class TestReasonCodes:
    """MQTT 5.0 — Reason Codes (§3.x for each packet type).

    MQTT 5.0 standardizes reason codes across all acknowledgment packets.
    §3.2.2.2 (CONNACK), §3.4.2.1 (PUBACK), §3.5.2.1 (PUBREC),
    §3.6.2.1 (PUBREL), §3.7.2.1 (PUBCOMP), §3.9.2.1 (SUBACK),
    §3.11.2.1 (UNSUBACK), §3.14.2.1 (DISCONNECT), §3.15.2.1 (AUTH).
    """

    # §2.4 の独立転記 — 実装の enum から導かず、数値と名前の対応を検証する。
    # ReasonCode.SUCCESS の正準ラベルは 'Success' で、DISCONNECT では
    # "Normal disconnection" とも読まれる。
    TABLE_2_9 = [
        (0x00, 'Success'),
        (0x01, 'Granted QoS 1'),
        (0x02, 'Granted QoS 2'),
        (0x04, 'Disconnect with Will Message'),
        (0x10, 'No matching subscribers'),  # PUBACK/PUBREC reason code
        (0x11, 'No subscription existed'),  # UNSUBACK
        (0x18, 'Continue authentication'),  # AUTH
        (0x19, 'Re-authenticate'),          # AUTH
        (0x80, 'Unspecified error'),
        (0x81, 'Malformed Packet'),
        (0x82, 'Protocol Error'),
        (0x83, 'Implementation specific error'),
        (0x84, 'Unsupported Protocol Version'),
        (0x85, 'Client Identifier not valid'),
        (0x86, 'Bad User Name or Password'),
        (0x87, 'Not authorized'),
        (0x88, 'Server unavailable'),
        (0x89, 'Server busy'),
        (0x8A, 'Banned'),
        (0x8B, 'Server shutting down'),
        (0x8C, 'Bad authentication method'),
        (0x8D, 'Keep Alive timeout'),
        (0x8E, 'Session taken over'),
        (0x8F, 'Topic Filter invalid'),
        (0x90, 'Topic Name invalid'),
        (0x91, 'Packet Identifier in use'),
        (0x92, 'Packet Identifier not found'),
        (0x93, 'Receive Maximum exceeded'),
        (0x94, 'Topic Alias invalid'),
        (0x95, 'Packet too large'),
        (0x96, 'Message rate too high'),
        (0x97, 'Quota exceeded'),
        (0x98, 'Administrative action'),
        (0x99, 'Payload format invalid'),
        (0x9A, 'Retain not supported'),
        (0x9B, 'QoS not supported'),
        (0x9C, 'Use another server'),
        (0x9D, 'Server moved'),
        (0x9E, 'Shared Subscriptions not supported'),
        (0x9F, 'Connection rate exceeded'),
        (0xA0, 'Maximum connect time'),
        (0xA1, 'Subscription Identifiers not supported'),
        (0xA2, 'Wildcard Subscriptions not supported'),
    ]

    @pytest.mark.parametrize("code,expected_name", TABLE_2_9)
    def test_reason_code_recognized(self, code, expected_name):
        """All MQTT 5.0 reason codes are recognized by name."""
        rc = MQTTReasonCode(code)
        assert rc.name == expected_name

    def test_the_table_covers_the_whole_registry(self):
        assert {code for code, _ in self.TABLE_2_9} == \
            {int(rc) for rc in ReasonCode}

    def test_a_code_below_unspecified_error_reports_success(self):
        for code in (ReasonCode.SUCCESS, ReasonCode.GRANTED_QOS_1,
                     ReasonCode.GRANTED_QOS_2, ReasonCode.DISCONNECT_WITH_WILL,
                     ReasonCode.NO_MATCHING_SUBSCRIBERS,
                     ReasonCode.NO_SUBSCRIPTION_EXISTED,
                     ReasonCode.CONTINUE_AUTHENTICATION,
                     ReasonCode.REAUTHENTICATE):
            assert MQTTReasonCode(int(code)).is_success

    def test_a_code_from_unspecified_error_up_reports_error(self):
        for code in (ReasonCode.UNSPECIFIED_ERROR, ReasonCode.MALFORMED_PACKET,
                     ReasonCode.PROTOCOL_ERROR,
                     ReasonCode.TOPIC_FILTER_INVALID,
                     ReasonCode.PAYLOAD_FORMAT_INVALID):
            assert MQTTReasonCode(int(code)).is_error

    def test_unknown_reason_code_handled(self):
        """Undefined reason codes should not crash but return 'Unknown'."""
        rc = MQTTReasonCode(0x50)  # 0x50 には定義済みの名前が無い
        assert rc.name.startswith('Unknown') or rc.is_error is None


# ============================================================================
# Dataclass construction defaults
# ============================================================================

class TestMessageDefaults:
    """Verify sensible defaults for all MQTT message dataclasses."""

    def test_connect_defaults(self):
        c = MQTTConnect(client_id='test', clean_start=True, keep_alive=60)
        assert c.username is None
        assert c.password is None
        assert c.will_topic is None
        assert c.properties == {}

    def test_publish_defaults(self):
        p = MQTTPublish(topic='test', payload=b'data', qos=0)
        assert p.retain is False
        assert p.dup is False
        assert p.packet_id is None
        assert p.properties == {}

    def test_disconnect_defaults(self):
        d = MQTTDisconnect()
        assert d.reason_code is None  # optional in MQTT 5.0
        assert d.properties == {}

    @pytest.mark.parametrize('cls', [
        pytest.param(MQTTPingreq, id='pingreq-defaults'),
        pytest.param(MQTTPingresp, id='pingresp-defaults'),
    ])
    def test_pingreq_defaults(self, cls):
        p = cls()
        assert p is not None


class TestProtocolLevel:
    """The CONNECT Protocol Level is a wire octet, not an enum membership."""

    def test_an_unnamed_level_still_decodes(self):
        """§3.1.2.2 — a level ``ProtocolLevel`` does not name must survive the
        codec: the broker answers CONNACK 0x84 for it rather than failing to
        read the packet."""
        message = MQTTConnect(client_id='x', clean_start=True, keep_alive=60,
                              proto_level=6)
        decoded = decode_packet(encode_packet(message))[0]
        assert decoded.proto_level == 6
        assert type(decoded.proto_level) is int

    def test_the_field_is_annotated_int(self):
        """The annotation is the contract: an unnamed level is legal input, so
        narrowing the field to ``ProtocolLevel`` is a defect even though plain
        pytest would not fault the value."""
        assert get_type_hints(MQTTConnect)['proto_level'] is int

    def test_the_default_is_the_wire_number(self):
        """The field is annotated ``int``, so the default is one."""
        default = MQTTConnect(client_id='x', clean_start=True, keep_alive=60)
        assert default.proto_level == ProtocolLevel.V5_0
        assert type(default.proto_level) is int


# ============================================================================
# Dataclass equality and immutability
# ============================================================================

class TestMessageEquality:
    """Value equality for MQTT message dataclasses."""

    def test_connect_equality(self):
        c1 = MQTTConnect(client_id='a', clean_start=True, keep_alive=60)
        c2 = MQTTConnect(client_id='a', clean_start=True, keep_alive=60)
        c3 = MQTTConnect(client_id='b', clean_start=True, keep_alive=60)
        assert c1 == c2
        assert c1 != c3

    def test_publish_equality(self):
        p1 = MQTTPublish(topic='t', payload=b'x', qos=1, packet_id=5)
        p2 = MQTTPublish(topic='t', payload=b'x', qos=1, packet_id=5)
        p3 = MQTTPublish(topic='t', payload=b'y', qos=1, packet_id=5)
        assert p1 == p2
        assert p1 != p3

    def test_suback_equality(self):
        s1 = MQTTSuback(packet_id=5, reason_codes=[ReasonCode.SUCCESS, ReasonCode.GRANTED_QOS_1])
        s2 = MQTTSuback(packet_id=5, reason_codes=[ReasonCode.SUCCESS, ReasonCode.GRANTED_QOS_1])
        s3 = MQTTSuback(packet_id=5, reason_codes=[ReasonCode.SUCCESS, ReasonCode.GRANTED_QOS_2])
        assert s1 == s2
        assert s1 != s3


class TestMessageImmutability:
    """Dataclasses should be frozen (immutable) to prevent accidental mutation."""

    def test_publish_is_frozen(self):
        p = MQTTPublish(topic='t', payload=b'x', qos=0)
        with pytest.raises(Exception):  # FrozenInstanceError or AttributeError
            p.topic = 'new'  # type: ignore[misc]

    def test_connect_is_frozen(self):
        c = MQTTConnect(client_id='test', clean_start=True, keep_alive=60)
        with pytest.raises(Exception):
            c.client_id = 'new'  # type: ignore[misc]


# ============================================================================
# Topic matching function (unit tests for pure algorithm)
# ============================================================================

class TestTopicMatchFunction:
    """``topic_matches_filter`` algorithm — exhaustive cases."""

    @pytest.mark.parametrize("topic,filter_str,expected", [
        # Literal exact match
        ('a/b/c', 'a/b/c', True),
        ('a/b/c', 'a/b/d', False),
        # Single-level wildcard '+'
        ('a/b/c', 'a/+/c', True),
        ('a/b/c', 'a/+/d', False),
        ('a/b/c', '+/+/+', True),
        ('a/b', '+/+', True),
        ('a', '+', True),
        ('a/b', '+', False),       # '+' matches exactly one level
        # Multi-level wildcard '#'
        ('a/b/c', 'a/#', True),
        ('a', 'a/#', True),        # '#' matches zero levels
        ('a/b/c', 'a/b/#', True),
        ('a/b/c', 'b/#', False),
        # '#' as entire filter
        ('anything', '#', True),
        # '$' prefixed topics (§4.7.2)
        ('$SYS/uptime', '#', False),
        ('$SYS/uptime', '+/uptime', False),
        ('$SYS/uptime', '$SYS/#', True),   # literal $ match
        # Shared subscriptions
        ('a/b', '$share/grp/a/b', True),
        # Empty topic
        ('', '#', False),  # empty string not a valid topic name
    ])
    def test_topic_matches_filter(self, topic, filter_str, expected):
        from blackbull.mqtt.messages import topic_matches_filter
        result = topic_matches_filter(topic, filter_str)
        assert result == expected, \
            f"topic_matches_filter({topic!r}, {filter_str!r}) = {result}, expected {expected}"
