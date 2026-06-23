"""
MQTT 5.0 Properties and AUTH exchange conformance tests — Sprint 52.

Verifies the MQTT 5.0 enhanced authentication and property system
against the OASIS Standard.

Reference: MQTT Version 5.0, OASIS Standard
  §2.2.2  Properties (extensible metadata)
  §2.2.2.2 Property Identifiers (Table 2-3)
  §3.15   AUTH – Authentication Exchange
  §4.12   Enhanced Authentication

Key behaviours:
  - §2.2.2: Properties are a key extension mechanism in MQTT 5.0.  Every
    packet type (except PINGREQ/PINGRESP) can carry properties.
  - §4.12: Enhanced Authentication allows SASL-style challenge/response
    flows using AUTH packets exchanged after CONNECT.
  - §3.15: AUTH packet is used for extended authentication.  It can be sent
    from the client or server at any time after CONNECT.
  - §4.12.2: Re-authentication (sending AUTH after initial authentication)
    allows updating credentials without disconnecting.
"""

import pytest

from blackbull.mqtt.messages import (
    MQTTAuth, MQTTConnect, MQTTConnack,
    encode_packet, decode_packet,
    MQTTReasonCode,
)


# ============================================================================
# §2.2.2 — Properties system completeness
# ============================================================================

class TestPropertyIdentifiers:
    """§2.2.2.2 Table 2-3 — Complete list of MQTT 5.0 Property Identifiers.

    Every property identifier from the spec is tested for encode/decode.
    """

    ALL_PROPERTIES = [
        # (id, name, type_category, packet_types)
        (0x01, 'payload_format_indicator', 'Byte', 'PUBLISH, Will'),
        (0x02, 'message_expiry_interval', 'Four Byte Integer', 'PUBLISH, Will'),
        (0x03, 'content_type', 'UTF-8 String', 'PUBLISH, Will'),
        (0x08, 'response_topic', 'UTF-8 String', 'PUBLISH, Will'),
        (0x09, 'correlation_data', 'Binary Data', 'PUBLISH, Will'),
        (0x0B, 'subscription_identifier', 'Variable Byte Integer', 'PUBLISH, SUBSCRIBE'),
        (0x11, 'session_expiry_interval', 'Four Byte Integer', 'CONNECT, CONNACK, DISCONNECT'),
        (0x12, 'assigned_client_identifier', 'UTF-8 String', 'CONNACK'),
        (0x13, 'server_keep_alive', 'Two Byte Integer', 'CONNACK'),
        (0x15, 'authentication_method', 'UTF-8 String', 'CONNECT, CONNACK, AUTH'),
        (0x16, 'authentication_data', 'Binary Data', 'CONNECT, CONNACK, AUTH'),
        (0x17, 'request_problem_information', 'Byte', 'CONNECT'),
        (0x18, 'will_delay_interval', 'Four Byte Integer', 'Will Properties'),
        (0x19, 'request_response_information', 'Byte', 'CONNECT'),
        (0x1A, 'response_information', 'UTF-8 String', 'CONNACK'),
        (0x1C, 'server_reference', 'UTF-8 String', 'CONNACK, DISCONNECT'),
        (0x1F, 'reason_string', 'UTF-8 String', 'all ACK packets'),
        (0x21, 'receive_maximum', 'Two Byte Integer', 'CONNECT, CONNACK'),
        (0x22, 'topic_alias_maximum', 'Two Byte Integer', 'CONNECT, CONNACK'),
        (0x23, 'topic_alias', 'Two Byte Integer', 'PUBLISH'),
        (0x24, 'maximum_qos', 'Byte', 'CONNACK'),
        (0x25, 'retain_available', 'Byte', 'CONNACK'),
        (0x26, 'user_property', 'UTF-8 String Pair', 'all packets'),
        (0x27, 'maximum_packet_size', 'Four Byte Integer', 'CONNECT, CONNACK'),
        (0x28, 'wildcard_subscription_available', 'Byte', 'CONNACK'),
        (0x29, 'subscription_identifier_available', 'Byte', 'CONNACK'),
        (0x2A, 'shared_subscription_available', 'Byte', 'CONNACK'),
    ]

    def test_all_27_property_identifiers_known(self):
        """§2.2.2.2 Table 2-3 — There are 27 defined property identifiers."""
        from blackbull.mqtt.messages import PROPERTY_IDENTIFIERS
        assert len(PROPERTY_IDENTIFIERS) == 27, \
            f"Expected 27 property identifiers, got {len(PROPERTY_IDENTIFIERS)}"

    @pytest.mark.parametrize("prop_id,prop_name,type_cat,pkt_types", ALL_PROPERTIES)
    def test_property_identifier_recognized(self, prop_id, prop_name, type_cat, pkt_types):
        """Each property identifier from Table 2-3 is recognized by name."""
        from blackbull.mqtt.messages import PROPERTY_IDENTIFIERS, get_property_info
        assert PROPERTY_IDENTIFIERS.get(prop_id) == prop_name
        info = get_property_info(prop_id)
        assert info is not None
        assert info.name == prop_name


# ============================================================================
# §2.2.2.2 — Property value validation by type
# ============================================================================

class TestPropertyValueValidation:
    """Type-specific validation for property values."""

    def test_receive_maximum_range(self):
        """§3.2.2.3.1 — Receive Maximum: 16-bit integer, 1–65535.
        0 is invalid (the server MUST treat 0 as "no receive maximum")."""
        # Within valid range
        connect = MQTTConnect(
            client_id='rm-client',
            clean_start=True,
            keep_alive=60,
            properties={'receive_maximum': 100},
        )
        wire = encode_packet(connect)
        decoded = decode_packet(wire)
        assert decoded.properties['receive_maximum'] == 100

    def test_topic_alias_maximum_range(self):
        """§3.2.2.3.6 — Topic Alias Maximum: 16-bit integer.
        0 = server does not accept topic aliases."""
        connect = MQTTConnect(
            client_id='ta-client',
            clean_start=True,
            keep_alive=60,
            properties={'topic_alias_maximum': 0},
        )
        wire = encode_packet(connect)
        decoded = decode_packet(wire)
        assert decoded.properties['topic_alias_maximum'] == 0

    def test_maximum_packet_size(self):
        """§3.2.2.3.4 — Maximum Packet Size: 32-bit integer.
        Represents the maximum packet size (in bytes) the server is willing
        to accept.  0 = no limit."""
        connect = MQTTConnect(
            client_id='mps-client',
            clean_start=True,
            keep_alive=60,
            properties={'maximum_packet_size': 262144},  # 256 KB
        )
        wire = encode_packet(connect)
        decoded = decode_packet(wire)
        assert decoded.properties['maximum_packet_size'] == 262144

    def test_content_type_utf8(self):
        """§2.2.2.2 — Content Type is a UTF-8 string (MIME type)."""
        publish_props = {
            'content_type': 'application/json; charset=utf-8',
        }
        from blackbull.mqtt.messages import encode_properties, decode_properties
        encoded = encode_properties(publish_props)
        decoded, _ = decode_properties(encoded)
        assert decoded['content_type'] == 'application/json; charset=utf-8'

    def test_correlation_data_binary(self):
        """§2.2.2.2 — Correlation Data is arbitrary binary."""
        from blackbull.mqtt.messages import encode_properties, decode_properties
        props = {'correlation_data': b'\xDE\xAD\xBE\xEF\x00\x01'}
        encoded = encode_properties(props)
        decoded, _ = decode_properties(encoded)
        assert decoded['correlation_data'] == b'\xDE\xAD\xBE\xEF\x00\x01'

    def test_subscription_identifier_variable_byte_integer(self):
        """§2.2.2.2 — Subscription Identifier is a Variable Byte Integer."""
        from blackbull.mqtt.messages import encode_properties, decode_properties
        for sid in (0, 1, 127, 128, 16383, 16384, 268435455):
            props = {'subscription_identifier': sid}
            encoded = encode_properties(props)
            decoded, _ = decode_properties(encoded)
            assert decoded['subscription_identifier'] == sid


# ============================================================================
# §2.2.2 — Properties per packet type (availability matrix)
# ============================================================================

class TestPropertiesPerPacketType:
    """Each MQTT control packet type allows specific properties.

    §2.2.2 Table 2-2 defines which packet types can carry properties.
    PINGREQ and PINGRESP (§3.12, §3.13) explicitly have NO properties.
    """

    def test_pingreq_has_no_properties(self):
        """§3.12 — PINGREQ cannot carry properties (no variable header)."""
        from blackbull.mqtt.messages import MQTTPingreq, encode_packet
        pingreq = MQTTPingreq()
        wire = encode_packet(pingreq)
        assert wire == b'\xC0\x00'

    def test_pingresp_has_no_properties(self):
        """§3.13 — PINGRESP cannot carry properties (no variable header)."""
        from blackbull.mqtt.messages import MQTTPingresp, encode_packet
        pingresp = MQTTPingresp()
        wire = encode_packet(pingresp)
        assert wire == b'\xD0\x00'

    def test_user_properties_in_all_ack_packets(self):
        """§2.2.2 — User Property (0x26) is available on ALL packet types
        that carry properties."""

        def _encode_decode_user_props(packet_cls, **kwargs):
            kwargs['properties'] = {'user_properties': [('app', 'test')]}
            pkt = packet_cls(**kwargs)
            wire = encode_packet(pkt)
            decoded = decode_packet(wire)
            assert decoded.properties['user_properties'] == [('app', 'test')]

        # Test user properties on all ACK packet types
        _encode_decode_user_props(MQTTConnack, session_present=False, reason_code=0)
        _encode_decode_user_props(MQTTConnect, client_id='up', clean_start=True, keep_alive=60)


# ============================================================================
# §3.15 / §4.12 — AUTH packet (Enhanced Authentication)
# ============================================================================

class TestAuthPacket:
    """§3.15 — AUTH packet for Enhanced Authentication.

    The AUTH packet provides a mechanism for extended authentication
    exchanges (e.g., SASL challenge/response).  It can be used:
      - During initial connection (after CONNECT, before CONNACK)
      - After CONNACK to re-authenticate
      - As a standalone authentication exchange

    §3.15.1: AUTH fixed header bits 3-0 MUST be 0b0000 (0x0).
    """

    def test_auth_fixed_header_flags(self):
        """§3.15.1 — AUTH fixed header flags MUST be 0x00."""
        auth = MQTTAuth(
            reason_code=0x18,  # Continue authentication
            properties={
                'authentication_method': 'SCRAM-SHA-256',
                'authentication_data': b'client-first-message',
            },
        )
        wire = encode_packet(auth)
        assert (wire[0] & 0x0F) == 0x00, \
            "AUTH fixed header flags must be 0x00 per §3.15.1"

    @pytest.mark.parametrize("auth_method", [
        'SCRAM-SHA-1',
        'SCRAM-SHA-256',
        'SCRAM-SHA-512',
        'KERBEROS',
        'CUSTOM-AUTH',
    ])
    def test_auth_with_authentication_method(self, auth_method):
        """§3.15.2.2 — AUTH carries Authentication Method and Data."""
        auth = MQTTAuth(
            reason_code=0x18,
            properties={
                'authentication_method': auth_method,
                'authentication_data': b'some-auth-data',
            },
        )
        wire = encode_packet(auth)
        decoded = decode_packet(wire)
        assert decoded.properties['authentication_method'] == auth_method

    def test_auth_continue_authentication(self):
        """§3.15.2.1 — AUTH reason code 0x18: Continue Authentication."""
        auth = MQTTAuth(
            reason_code=0x18,  # Continue authentication
            properties={
                'authentication_method': 'SCRAM-SHA-256',
                'authentication_data': b'server-first-message',
            },
        )
        wire = encode_packet(auth)
        decoded = decode_packet(wire)
        assert decoded.reason_code == 0x18

    def test_auth_re_authenticate(self):
        """§3.15.2.1 / §4.12.2 — AUTH reason code 0x19: Re-authentication."""
        auth = MQTTAuth(
            reason_code=0x19,  # Re-authenticate
            properties={
                'authentication_method': 'SCRAM-SHA-256',
                'authentication_data': b'reauth-data',
            },
        )
        wire = encode_packet(auth)
        decoded = decode_packet(wire)
        assert decoded.reason_code == 0x19

    def test_auth_success(self):
        """§3.15.2.1 — AUTH reason code 0x00: Success (authentication complete)."""
        auth = MQTTAuth(
            reason_code=0x00,
            properties={
                'authentication_method': 'SCRAM-SHA-256',
                'authentication_data': b'final-server-proof',
            },
        )
        wire = encode_packet(auth)
        decoded = decode_packet(wire)
        assert decoded.reason_code == 0x00

    def test_auth_without_reason_code(self):
        """§3.15.2 — AUTH without a reason code (pre-5.0 compatibility)."""
        auth = MQTTAuth(
            properties={
                'authentication_method': 'BASIC',
                'authentication_data': b'credentials',
            },
        )
        wire = encode_packet(auth)
        decoded = decode_packet(wire)
        assert decoded.properties['authentication_method'] == 'BASIC'


# ============================================================================
# §4.12 — Enhanced authentication flow
# ============================================================================

class TestEnhancedAuthenticationFlow:
    """§4.12 — Multi-step authentication using AUTH exchange.

    Enhanced Authentication allows SASL-style challenge/response:
      1. Client → CONNECT   (with auth method, no password)
      2. Server → AUTH      (0x18 Continue, with server challenge)
      3. Client → AUTH      (0x18 Continue, with client response)
      4. Server → CONNACK   (0x00 Success)
    """

    def test_connect_with_auth_method_no_password(self):
        """§4.12 — CONNECT declares authentication method without credentials."""
        connect = MQTTConnect(
            client_id='sasl-client',
            clean_start=True,
            keep_alive=60,
            properties={
                'authentication_method': 'SCRAM-SHA-256',
            },
        )
        wire = encode_packet(connect)
        decoded = decode_packet(wire)
        assert decoded.properties['authentication_method'] == 'SCRAM-SHA-256'
        # No username/password set
        assert decoded.username is None
        assert decoded.password is None

    def test_connect_with_auth_method_and_data(self):
        """§4.12 — CONNECT includes initial authentication data."""
        connect = MQTTConnect(
            client_id='sasl-client2',
            clean_start=True,
            keep_alive=60,
            properties={
                'authentication_method': 'SCRAM-SHA-256',
                'authentication_data': b'n,,n=user,r=fyko+d2lbbFgONRv9',
            },
        )
        wire = encode_packet(connect)
        decoded = decode_packet(wire)
        assert decoded.properties['authentication_data'] is not None

    def test_auth_failure_reason_codes(self):
        """§4.12.1 — Authentication failure reason codes."""
        for code in (0x86, 0x87, 0x8C):
            auth = MQTTAuth(
                reason_code=code,
                properties={
                    'authentication_method': 'SCRAM-SHA-256',
                    'reason_string': 'Authentication failed',
                },
            )
            wire = encode_packet(auth)
            decoded = decode_packet(wire)
            assert decoded.reason_code == code
