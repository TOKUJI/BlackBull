"""
MQTT 5.0 message dataclass unit tests — Sprint 52.

Tests individual MQTT message dataclasses in isolation (no I/O, no Actor).
Verifies that message objects behave correctly (construction, equality,
immutability, edge cases).

Reference: MQTT Version 5.0, OASIS Standard
"""

import pytest

from blackbull.mqtt.messages import (
    # Control packet dataclasses
    MQTTConnect, MQTTConnack,
    MQTTPublish, MQTTPuback, MQTTPubrec, MQTTPubrel, MQTTPubcomp,
    MQTTSubscribe, MQTTSuback,
    MQTTUnsubscribe, MQTTUnsuback,
    MQTTPingreq, MQTTPingresp,
    MQTTDisconnect, MQTTAuth,
    # Enums
    MQTTPacketType, MQTTReasonCode,
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
    def test_packet_type_name(self, ptype, name):
        assert MQTTPacketType(ptype).name == name

    def test_packet_type_from_raw_byte(self):
        """Extract type from first byte of fixed header (bits 7-4)."""
        # CONNECT: 0x10 → type=1 (0x10 >> 4)
        assert MQTTPacketType(0x10 >> 4) == MQTTPacketType.CONNECT
        # PUBREL: 0x62 → type=6 ((0x62 >> 4) & 0x0F)
        assert MQTTPacketType((0x62 >> 4) & 0x0F) == MQTTPacketType.PUBREL


# ============================================================================
# MQTTReasonCode enum
# ============================================================================

class TestReasonCodeEnum:
    """MQTTReasonCode enum — all MQTT 5.0 reason codes."""

    def test_success_is_0x00(self):
        assert MQTTReasonCode(0x00).name == 'Success'
        assert MQTTReasonCode(0x00).is_success is True
        assert MQTTReasonCode(0x00).is_error is False

    def test_error_codes_ge_0x80(self):
        for code in (0x80, 0x81, 0x87, 0x8F, 0x99):
            reason = MQTTReasonCode(code)
            assert reason.is_error is True
            assert reason.is_success is False

    def test_unknown_reason_code_handled(self):
        """Undefined reason codes should not crash but return 'Unknown'."""
        rc = MQTTReasonCode(0x50)  # 0x50 is not a defined MQTT 5.0 reason code
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

    def test_pingreq_defaults(self):
        p = MQTTPingreq()
        assert p is not None

    def test_pingresp_defaults(self):
        p = MQTTPingresp()
        assert p is not None


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
        s1 = MQTTSuback(packet_id=5, reason_codes=[0, 1])
        s2 = MQTTSuback(packet_id=5, reason_codes=[0, 1])
        s3 = MQTTSuback(packet_id=5, reason_codes=[0, 2])
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
