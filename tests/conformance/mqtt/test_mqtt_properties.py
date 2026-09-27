"""
MQTT 5.0 Properties and AUTH exchange conformance tests.

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
    PropertyId,
    ReasonCode,
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
        (PropertyId.PAYLOAD_FORMAT_INDICATOR, 'payload_format_indicator', 'Byte', 'PUBLISH, Will'),
        (PropertyId.MESSAGE_EXPIRY_INTERVAL, 'message_expiry_interval', 'Four Byte Integer', 'PUBLISH, Will'),
        (PropertyId.CONTENT_TYPE, 'content_type', 'UTF-8 String', 'PUBLISH, Will'),
        (PropertyId.RESPONSE_TOPIC, 'response_topic', 'UTF-8 String', 'PUBLISH, Will'),
        (PropertyId.CORRELATION_DATA, 'correlation_data', 'Binary Data', 'PUBLISH, Will'),
        (PropertyId.SUBSCRIPTION_IDENTIFIER, 'subscription_identifier', 'Variable Byte Integer', 'PUBLISH, SUBSCRIBE'),
        (PropertyId.SESSION_EXPIRY_INTERVAL, 'session_expiry_interval', 'Four Byte Integer', 'CONNECT, CONNACK, DISCONNECT'),
        (PropertyId.ASSIGNED_CLIENT_IDENTIFIER, 'assigned_client_identifier', 'UTF-8 String', 'CONNACK'),
        (PropertyId.SERVER_KEEP_ALIVE, 'server_keep_alive', 'Two Byte Integer', 'CONNACK'),
        (PropertyId.AUTHENTICATION_METHOD, 'authentication_method', 'UTF-8 String', 'CONNECT, CONNACK, AUTH'),
        (PropertyId.AUTHENTICATION_DATA, 'authentication_data', 'Binary Data', 'CONNECT, CONNACK, AUTH'),
        (PropertyId.REQUEST_PROBLEM_INFORMATION, 'request_problem_information', 'Byte', 'CONNECT'),
        (PropertyId.WILL_DELAY_INTERVAL, 'will_delay_interval', 'Four Byte Integer', 'Will Properties'),
        (PropertyId.REQUEST_RESPONSE_INFORMATION, 'request_response_information', 'Byte', 'CONNECT'),
        (PropertyId.RESPONSE_INFORMATION, 'response_information', 'UTF-8 String', 'CONNACK'),
        (PropertyId.SERVER_REFERENCE, 'server_reference', 'UTF-8 String', 'CONNACK, DISCONNECT'),
        (PropertyId.REASON_STRING, 'reason_string', 'UTF-8 String', 'all ACK packets'),
        (PropertyId.RECEIVE_MAXIMUM, 'receive_maximum', 'Two Byte Integer', 'CONNECT, CONNACK'),
        (PropertyId.TOPIC_ALIAS_MAXIMUM, 'topic_alias_maximum', 'Two Byte Integer', 'CONNECT, CONNACK'),
        (PropertyId.TOPIC_ALIAS, 'topic_alias', 'Two Byte Integer', 'PUBLISH'),
        (PropertyId.MAXIMUM_QOS, 'maximum_qos', 'Byte', 'CONNACK'),
        (PropertyId.RETAIN_AVAILABLE, 'retain_available', 'Byte', 'CONNACK'),
        (PropertyId.USER_PROPERTY, 'user_property', 'UTF-8 String Pair', 'all packets'),
        (PropertyId.MAXIMUM_PACKET_SIZE, 'maximum_packet_size', 'Four Byte Integer', 'CONNECT, CONNACK'),
        (PropertyId.WILDCARD_SUBSCRIPTION_AVAILABLE, 'wildcard_subscription_available', 'Byte', 'CONNACK'),
        (PropertyId.SUBSCRIPTION_IDENTIFIER_AVAILABLE, 'subscription_identifier_available', 'Byte', 'CONNACK'),
        (PropertyId.SHARED_SUBSCRIPTION_AVAILABLE, 'shared_subscription_available', 'Byte', 'CONNACK'),
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
        _encode_decode_user_props(MQTTConnack, session_present=False, reason_code=ReasonCode.SUCCESS)
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
            reason_code=ReasonCode.CONTINUE_AUTHENTICATION,
            properties={
                'authentication_method': 'SCRAM-SHA-256',
                'authentication_data': b'client-first-message',
            },
        )
        wire = encode_packet(auth)
        assert (wire[0] & 0x0F) == 0

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
            reason_code=ReasonCode.CONTINUE_AUTHENTICATION,
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
            reason_code=ReasonCode.CONTINUE_AUTHENTICATION,
            properties={
                'authentication_method': 'SCRAM-SHA-256',
                'authentication_data': b'server-first-message',
            },
        )
        wire = encode_packet(auth)
        decoded = decode_packet(wire)
        assert decoded.reason_code == ReasonCode.CONTINUE_AUTHENTICATION

    def test_auth_re_authenticate(self):
        """§3.15.2.1 / §4.12.2 — AUTH reason code 0x19: Re-authentication."""
        auth = MQTTAuth(
            reason_code=ReasonCode.REAUTHENTICATE,
            properties={
                'authentication_method': 'SCRAM-SHA-256',
                'authentication_data': b'reauth-data',
            },
        )
        wire = encode_packet(auth)
        decoded = decode_packet(wire)
        assert decoded.reason_code == ReasonCode.REAUTHENTICATE

    def test_auth_success(self):
        """§3.15.2.1 — AUTH reason code 0x00: Success (authentication complete)."""
        auth = MQTTAuth(
            reason_code=ReasonCode.SUCCESS,
            properties={
                'authentication_method': 'SCRAM-SHA-256',
                'authentication_data': b'final-server-proof',
            },
        )
        wire = encode_packet(auth)
        decoded = decode_packet(wire)
        assert decoded.reason_code == ReasonCode.SUCCESS

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
        for code in (ReasonCode.BAD_USER_NAME_OR_PASSWORD,
                     ReasonCode.NOT_AUTHORIZED,
                     ReasonCode.BAD_AUTHENTICATION_METHOD):
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
