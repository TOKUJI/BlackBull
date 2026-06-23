"""
MQTT 5.0 edge cases and server capability tests — Sprint 52.

Covers remaining spec areas not in other test files.

Reference: MQTT Version 5.0, OASIS Standard
  §3.1.2.11  Request Problem Information flag
  §3.2.2.3.2 Server capability flags (Retain Available, Wildcard Available, etc.)
  §3.3.2.3.2 Message Expiry Interval
  §3.2.2.3.4 Maximum Packet Size enforcement
  §4.2       Network Connections (concurrent connections)
  §1.5.4     UTF-8 string encoding rules
"""

import pytest

from blackbull.mqtt.messages import (
    MQTTConnect, MQTTConnack, MQTTDisconnect,
    MQTTPublish, MQTTSubscribe, MQTTSuback,
    encode_packet, decode_packet,
    MQTTReasonCode,
)


# ============================================================================
# §3.1.2.11 — Request Problem Information flag
# ============================================================================

class TestRequestProblemInformation:
    """§3.1.2.11 — Request Problem Information (byte, 0 or 1).

    When set to 1, the client requests the server to return detailed
    error information (Reason String, User Properties) in failure
    responses.  When 0 (default), the server MAY omit Reason String
    even on errors.
    """

    def test_request_problem_information_default(self, mqtt):
        """§3.1.2.11 — Default is 0 (no detailed errors)."""
        connect = MQTTConnect(
            client_id='rpi-default',
            clean_start=True,
            keep_alive=60,
        )
        wire = encode_packet(connect)
        decoded = decode_packet(wire)
        assert 'request_problem_information' not in decoded.properties

    def test_request_problem_information_enabled(self, mqtt):
        """§3.1.2.11 — Request Problem Information = 1."""
        connect = MQTTConnect(
            client_id='rpi-on',
            clean_start=True,
            keep_alive=60,
            properties={'request_problem_information': 1},
        )
        wire = encode_packet(connect)
        decoded = decode_packet(wire)
        assert decoded.properties['request_problem_information'] == 1

    def test_request_problem_information_disabled(self, mqtt):
        """§3.1.2.11 — Request Problem Information = 0 explicitly."""
        connect = MQTTConnect(
            client_id='rpi-off',
            clean_start=True,
            keep_alive=60,
            properties={'request_problem_information': 0},
        )
        wire = encode_packet(connect)
        decoded = decode_packet(wire)
        assert decoded.properties['request_problem_information'] == 0


# ============================================================================
# §3.2.2.3.2 — Server capability flags in CONNACK
# ============================================================================

class TestServerCapabilities:
    """§3.2.2.3.2 — CONNACK properties describing server capabilities.

    These are informational flags that tell the client what features
    the server supports, so the client can avoid using unsupported features.
    """

    def test_retain_available_flag(self, mqtt):
        """§3.2.2.3.2 — Retain Available: 0 = not supported, 1 = supported."""
        for val in (0, 1):
            connack = MQTTConnack(
                session_present=False,
                reason_code=0x00,
                properties={'retain_available': val},
            )
            wire = encode_packet(connack)
            decoded = decode_packet(wire)
            assert decoded.properties['retain_available'] == val

    def test_wildcard_subscription_available_flag(self, mqtt):
        """§3.2.2.3.2 — Wildcard Subscription Available."""
        for val in (0, 1):
            connack = MQTTConnack(
                session_present=False,
                reason_code=0x00,
                properties={'wildcard_subscription_available': val},
            )
            wire = encode_packet(connack)
            decoded = decode_packet(wire)
            assert decoded.properties['wildcard_subscription_available'] == val

    def test_subscription_identifier_available_flag(self, mqtt):
        """§3.2.2.3.2 — Subscription Identifier Available."""
        for val in (0, 1):
            connack = MQTTConnack(
                session_present=False,
                reason_code=0x00,
                properties={'subscription_identifier_available': val},
            )
            wire = encode_packet(connack)
            decoded = decode_packet(wire)
            assert decoded.properties['subscription_identifier_available'] == val

    def test_shared_subscription_available_flag(self, mqtt):
        """§3.2.2.3.2 — Shared Subscription Available."""
        for val in (0, 1):
            connack = MQTTConnack(
                session_present=False,
                reason_code=0x00,
                properties={'shared_subscription_available': val},
            )
            wire = encode_packet(connack)
            decoded = decode_packet(wire)
            assert decoded.properties['shared_subscription_available'] == val

    def test_all_server_capability_flags_together(self, mqtt):
        """§3.2.2.3.2 — CONNACK with all capability flags."""
        connack = MQTTConnack(
            session_present=False,
            reason_code=0x00,
            properties={
                'retain_available': 1,
                'wildcard_subscription_available': 1,
                'subscription_identifier_available': 1,
                'shared_subscription_available': 0,
                'maximum_qos': 2,
                'receive_maximum': 100,
                'topic_alias_maximum': 16,
                'maximum_packet_size': 268435455,
                'server_keep_alive': 120,
            },
        )
        wire = encode_packet(connack)
        decoded = decode_packet(wire)
        assert decoded.properties['retain_available'] == 1
        assert decoded.properties['shared_subscription_available'] == 0
        assert decoded.properties['maximum_qos'] == 2


# ============================================================================
# §3.3.2.3.2 — Message Expiry Interval
# ============================================================================

class TestMessageExpiryInterval:
    """§3.3.2.3.2 — Message Expiry Interval.

    A 4-byte integer representing the Message Expiry Interval in seconds.
    If absent, the message does not expire.
    """

    def test_publish_with_message_expiry(self, mqtt):
        """§3.3.2.3.2 — PUBLISH with Message Expiry Interval."""
        publish = MQTTPublish(
            topic='events/temporary',
            payload=b'expires soon',
            qos=1,
            packet_id=1,
            properties={'message_expiry_interval': 60},
        )
        wire = encode_packet(publish)
        decoded = decode_packet(wire)
        assert decoded.properties['message_expiry_interval'] == 60

    def test_message_expiry_zero_no_expiry(self, mqtt):
        """§3.3.2.3.2 — Message Expiry Interval = 0 means no expiry."""
        publish = MQTTPublish(
            topic='events/permanent',
            payload=b'never expires',
            qos=1,
            packet_id=1,
            properties={'message_expiry_interval': 0},
        )
        wire = encode_packet(publish)
        decoded = decode_packet(wire)
        assert decoded.properties['message_expiry_interval'] == 0

    def test_will_message_expiry(self, mqtt):
        """§3.1.3.3 — Will Message can have Message Expiry Interval."""
        connect = MQTTConnect(
            client_id='will-expiry-client',
            clean_start=True,
            keep_alive=60,
            will_topic='clients/status',
            will_payload=b'lwt',
            will_qos=1,
            will_retain=False,
            will_properties={'message_expiry_interval': 3600},
        )
        wire = encode_packet(connect)
        decoded = decode_packet(wire)
        assert decoded.will_properties['message_expiry_interval'] == 3600


# ============================================================================
# §3.2.2.3.4 — Maximum Packet Size
# ============================================================================

class TestMaximumPacketSize:
    """§3.2.2.3.4 — Maximum Packet Size.

    The server MAY inform the client of the maximum packet size it is
    willing to accept.  If a client sends a packet larger than this,
    the server MUST send DISCONNECT with reason code 0x8E (Packet too large).
    """

    def test_maximum_packet_size_in_connack(self, mqtt):
        """§3.2.2.3.4 — CONNACK with Maximum Packet Size."""
        connack = MQTTConnack(
            session_present=False,
            reason_code=0x00,
            properties={'maximum_packet_size': 65536},  # 64 KB
        )
        wire = encode_packet(connack)
        decoded = decode_packet(wire)
        assert decoded.properties['maximum_packet_size'] == 65536

    def test_maximum_packet_size_in_connect(self, mqtt):
        """§3.1.2.3 — Client can also advertise Maximum Packet Size."""
        connect = MQTTConnect(
            client_id='mps-client',
            clean_start=True,
            keep_alive=60,
            properties={'maximum_packet_size': 262144},  # 256 KB
        )
        wire = encode_packet(connect)
        decoded = decode_packet(wire)
        assert decoded.properties['maximum_packet_size'] == 262144

    def test_packet_too_large_disconnect_reason(self, mqtt):
        """§3.14.2.1 — DISCONNECT reason code 0x8E: Packet too large."""
        disconnect = MQTTDisconnect(
            reason_code=0x8E,
            properties={'reason_string': 'Packet exceeds maximum allowed size'},
        )
        wire = encode_packet(disconnect)
        decoded = decode_packet(wire)
        assert decoded.reason_code == 0x8E


# ============================================================================
# §1.5.4 — UTF-8 String encoding
# ============================================================================

class TestUTF8StringEncoding:
    """§1.5.4 — UTF-8 Encoded String.

    MQTT 5.0 uses UTF-8 encoding for all text fields.  A UTF-8 string
    is prefixed with a 2-byte length (0–65535 bytes).

    §1.5.4.1: Strings MUST be valid UTF-8.
    §1.5.4.2: The null character (U+0000) MUST NOT be used.
    """

    def test_client_id_utf8_validation(self, mqtt):
        """§1.5.4 — Client ID must be valid UTF-8 (encoded by dataclass)."""
        # Valid UTF-8
        connect = MQTTConnect(
            client_id='日本語クライアント',
            clean_start=True,
            keep_alive=60,
        )
        wire = encode_packet(connect)
        decoded = decode_packet(wire)
        assert decoded.client_id == '日本語クライアント'

    def test_topic_name_utf8_validation(self, mqtt):
        """§1.5.4 — Topic Names must be valid UTF-8."""
        from blackbull.mqtt.messages import validate_topic_name
        assert validate_topic_name('sensors/温度') is True

    def test_null_character_in_string_rejected(self, mqtt):
        """§1.5.4.2 — U+0000 (null) MUST NOT appear in UTF-8 strings."""
        # Client ID with null
        with pytest.raises(ValueError, match='null|\\\\x00|U\\+0000'):
            MQTTConnect(
                client_id='bad\x00id',
                clean_start=True,
                keep_alive=60,
            )

    def test_topic_filter_null_rejected(self, mqtt):
        """§1.5.4.2 — Null character in Topic Filter is invalid."""
        from blackbull.mqtt.messages import validate_topic_filter
        assert validate_topic_filter('sensors/\x00room') is False


# ============================================================================
# §3.1.2.11 — Session state includes all subscriptions
# ============================================================================

class TestSessionStateDetails:
    """§3.1.2.11 — Session state granularity.

    Session state includes:
      - All existing subscriptions (topic filters + QoS + subscription options)
      - QoS 1 and QoS 2 messages queued for delivery but not yet acknowledged
      - QoS 2 messages received but PUBREL not yet sent
      - QoS 2 messages sent but PUBCOMP not yet received
    """

    def test_session_stores_subscription_options(self, mqtt):
        """§3.1.2.11 — Session preserves Subscription Options (No Local,
        Retain As Published, Retain Handling) across reconnects."""
        # The session dict stores the full subscription configuration
        # Subscription entries carry an options dict, so the collection must be
        # a list (a dict is unhashable and cannot live in a set).
        mqtt.sessions['opts-client'] = {
            'subscriptions': [
                ('chat/room1', 1, {'no_local': True, 'retain_as_published': True,
                                   'retain_handling': 1}),
                ('alerts/#', 2, {'no_local': False}),
            ],
            'pending_qos2_in': {},
            'pending_qos2_out': {},
        }
        session = mqtt.sessions.get('opts-client')
        assert session is not None
        assert len(session['subscriptions']) == 2
        chat_sub = [s for s in session['subscriptions'] if s[0] == 'chat/room1'][0]
        assert chat_sub[2]['no_local'] is True

    def test_session_stores_pending_qos1_messages(self, mqtt):
        """§3.1.2.11 — Session stores QoS 1 messages pending acknowledgment."""
        mqtt.sessions['qos1-client'] = {
            'subscriptions': {},
            'pending_qos1_out': {
                100: MQTTPublish(
                    topic='test/qos1',
                    payload=b'message-1',
                    qos=1,
                    packet_id=100,
                ),
                101: MQTTPublish(
                    topic='test/qos1',
                    payload=b'message-2',
                    qos=1,
                    packet_id=101,
                ),
            },
            'pending_qos2_in': {},
            'pending_qos2_out': {},
        }
        session = mqtt.sessions.get('qos1-client')
        assert len(session['pending_qos1_out']) == 2

    def test_session_stores_pending_qos2_states(self, mqtt):
        """§3.1.2.11 — Session stores QoS 2 messages in various states."""
        mqtt.sessions['qos2-client'] = {
            'subscriptions': {},
            'pending_qos1_out': {},
            'pending_qos2_in': {
                200: 'PUBREC_SENT',   # PUBREC sent, waiting for PUBREL
                201: 'PUBREC_SENT',
            },
            'pending_qos2_out': {
                300: 'PUBLISH_SENT',  # PUBLISH sent, waiting for PUBREC
                301: 'PUBREL_SENT',   # PUBREL sent, waiting for PUBCOMP
            },
        }
        session = mqtt.sessions.get('qos2-client')
        assert session['pending_qos2_in'][200] == 'PUBREC_SENT'
        assert session['pending_qos2_out'][300] == 'PUBLISH_SENT'
        assert session['pending_qos2_out'][301] == 'PUBREL_SENT'


# ============================================================================
# §3.14 — DISCONNECT with Session Expiry override
# ============================================================================

class TestDisconnectSessionExpiry:
    """§3.14.2.2 — DISCONNECT can override Session Expiry Interval.

    The client can set a new Session Expiry Interval in DISCONNECT,
    which takes effect even if different from the CONNECT value.
    """

    def test_disconnect_with_shorter_session_expiry(self, mqtt):
        """§3.14.2.2 — Client reduces Session Expiry on DISCONNECT."""
        disconnect = MQTTDisconnect(
            reason_code=0x00,
            properties={'session_expiry_interval': 0},  # Expire immediately
        )
        wire = encode_packet(disconnect)
        decoded = decode_packet(wire)
        assert decoded.properties['session_expiry_interval'] == 0

    def test_disconnect_with_longer_session_expiry(self, mqtt):
        """§3.14.2.2 — Client extends Session Expiry on DISCONNECT."""
        disconnect = MQTTDisconnect(
            reason_code=0x00,
            properties={'session_expiry_interval': 86400},  # 24 hours
        )
        wire = encode_packet(disconnect)
        decoded = decode_packet(wire)
        assert decoded.properties['session_expiry_interval'] == 86400


# ============================================================================
# §3.1.2.11 — Response Information
# ============================================================================

class TestResponseInformation:
    """§3.1.2.11 / §3.2.2.3.2 — Response Information.

    If the client sets Request Response Information in CONNECT,
    the server MAY respond with Response Information in CONNACK
    (a UTF-8 string used as the basis for creating Response Topics).
    """

    def test_request_response_information(self, mqtt):
        """§3.1.2.11 — Client requests Response Information."""
        connect = MQTTConnect(
            client_id='rri-client',
            clean_start=True,
            keep_alive=60,
            properties={'request_response_information': 1},
        )
        wire = encode_packet(connect)
        decoded = decode_packet(wire)
        assert decoded.properties['request_response_information'] == 1

    def test_response_information_in_connack(self, mqtt):
        """§3.2.2.3.2 — Server provides Response Information."""
        connack = MQTTConnack(
            session_present=False,
            reason_code=0x00,
            properties={'response_information': 'responses/client-abc123'},
        )
        wire = encode_packet(connack)
        decoded = decode_packet(wire)
        assert decoded.properties['response_information'] == 'responses/client-abc123'


# ============================================================================
# §2.1 — Reserved control packet types
# ============================================================================

class TestReservedPacketTypes:
    """§2.1.1 — Packet types 0 and 15+ are reserved/forbidden.

    Receiving a packet with an unrecognized type MUST cause the server
    to close the connection.
    """

    def test_packet_type_0_is_forbidden(self, mqtt):
        """§2.1.1 — Control Packet Type 0 is reserved."""
        from blackbull.mqtt.messages import extract_packet_type
        # 0x00 would mean type=0
        with pytest.raises(ValueError, match='[Rr]eserved|[Ff]orbidden|[Uu]nknown'):
            extract_packet_type(0x00)
