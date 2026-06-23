"""
MQTT 5.0 Control Packet structure tests — Sprint 52.

Tests for MQTT 5.0 packet encoding/decoding at the wire-format level.
Verifies compliance with MQTT Version 5.0 OASIS Standard.

Reference: MQTT Version 5.0, OASIS Standard
  https://docs.oasis-open.org/mqtt/mqtt/v5.0/os/mqtt-v5.0-os.html

Key sections covered:
  - §2.1   Structure of a Control Packet
  - §2.1.1 Fixed Header (Control Packet Type + Flags + Remaining Length)
  - §2.1.2 Variable Header (Packet Identifier + Properties)
  - §2.1.3 Payload
  - §2.2.1 Remaining Length (variable byte integer encoding)
  - §1.5.1 Variable Byte Integer (normative encoding rules)

Packet type values (§2.1.1 Table 2-1):
  1=CONNECT, 2=CONNACK, 3=PUBLISH, 4=PUBACK, 5=PUBREC, 6=PUBREL,
  7=PUBCOMP, 8=SUBSCRIBE, 9=SUBACK, 10=UNSUBSCRIBE, 11=UNSUBACK,
  12=PINGREQ, 13=PINGRESP, 14=DISCONNECT, 15=AUTH
"""

import pytest
import struct

# ---------------------------------------------------------------------------
# Expected module under test — MQTT 5 packet codec will live here:
#   blackbull/mqtt/messages.py
#
# For TDD, we import from the expected module (tests will fail until
# the implementation is written).  Where the module does not exist yet,
# we define the expected API as test-time constants.
# ---------------------------------------------------------------------------

# MQTT 5.0 Control Packet Type constants (§2.1.1 Table 2-1)
MQTT_PACKET_CONNECT     = 1
MQTT_PACKET_CONNACK     = 2
MQTT_PACKET_PUBLISH     = 3
MQTT_PACKET_PUBACK      = 4
MQTT_PACKET_PUBREC      = 5
MQTT_PACKET_PUBREL      = 6
MQTT_PACKET_PUBCOMP     = 7
MQTT_PACKET_SUBSCRIBE   = 8
MQTT_PACKET_SUBACK      = 9
MQTT_PACKET_UNSUBSCRIBE = 10
MQTT_PACKET_UNSUBACK    = 11
MQTT_PACKET_PINGREQ     = 12
MQTT_PACKET_PINGRESP    = 13
MQTT_PACKET_DISCONNECT  = 14
MQTT_PACKET_AUTH        = 15

# MQTT 5.0 Property Identifiers (§2.2.2.2 Table 2-3)
PROP_PAYLOAD_FORMAT_INDICATOR          = 0x01
PROP_MESSAGE_EXPIRY_INTERVAL           = 0x02
PROP_CONTENT_TYPE                      = 0x03
PROP_RESPONSE_TOPIC                    = 0x08
PROP_CORRELATION_DATA                  = 0x09
PROP_SUBSCRIPTION_IDENTIFIER           = 0x0B
PROP_SESSION_EXPIRY_INTERVAL           = 0x11
PROP_ASSIGNED_CLIENT_IDENTIFIER        = 0x12
PROP_SERVER_KEEP_ALIVE                 = 0x13
PROP_AUTHENTICATION_METHOD             = 0x15
PROP_AUTHENTICATION_DATA               = 0x16
PROP_REQUEST_PROBLEM_INFORMATION       = 0x17
PROP_WILL_DELAY_INTERVAL               = 0x18
PROP_REQUEST_RESPONSE_INFORMATION      = 0x19
PROP_RESPONSE_INFORMATION              = 0x1A
PROP_SERVER_REFERENCE                  = 0x1C
PROP_REASON_STRING                     = 0x1F
PROP_RECEIVE_MAXIMUM                   = 0x21
PROP_TOPIC_ALIAS_MAXIMUM               = 0x22
PROP_TOPIC_ALIAS                       = 0x23
PROP_MAXIMUM_QOS                       = 0x24
PROP_RETAIN_AVAILABLE                  = 0x25
PROP_USER_PROPERTY                     = 0x26
PROP_MAXIMUM_PACKET_SIZE               = 0x27
PROP_WILDCARD_SUBSCRIPTION_AVAILABLE   = 0x28
PROP_SUBSCRIPTION_IDENTIFIER_AVAILABLE = 0x29
PROP_SHARED_SUBSCRIPTION_AVAILABLE     = 0x2A


# ============================================================================
# §2.2.1 / §1.5.1 — Variable Byte Integer (Remaining Length) encoding
# ============================================================================

class TestVariableByteInteger:
    """MQTT 5.0 §1.5.1 / §2.2.1 — Variable Byte Integer encoding.

    The Remaining Length is encoded as a Variable Byte Integer, using one to
    four 7-bit bytes.  Each byte encodes 7 bits; the high bit (0x80) is a
    continuation flag.  The maximum representable value is 268,435,455.
    """

    # §1.5.1 — Encoding examples from the specification
    @pytest.mark.parametrize("value,expected_bytes", [
        (0,         b'\x00'),            # §1.5.1: 0 encodes as 0x00
        (127,       b'\x7f'),            # §1.5.1: 127 encodes as 0x7F
        (128,       b'\x80\x01'),        # §1.5.1: 128 encodes as 0x80 0x01
        (16383,     b'\xff\x7f'),        # §1.5.1: 16,383 encodes as 0xFF 0x7F
        (16384,     b'\x80\x80\x01'),    # §1.5.1: 16,384 encodes as 0x80 0x80 0x01
        (2097151,   b'\xff\xff\x7f'),    # §1.5.1: 2,097,151 = 0xFF 0xFF 0x7F
        (2097152,   b'\x80\x80\x80\x01'),# §1.5.1: 2,097,152 = 0x80 0x80 0x80 0x01
        (268435455, b'\xff\xff\xff\x7f'),# §1.5.1: 268,435,455 = 0xFF 0xFF 0xFF 0x7F
    ])
    def test_encode_variable_byte_integer(self, value, expected_bytes):
        """§1.5.1 — Encode integer value to Variable Byte Integer bytes."""
        from blackbull.mqtt.messages import encode_variable_byte_integer
        assert encode_variable_byte_integer(value) == expected_bytes

    @pytest.mark.parametrize("encoded_bytes,expected_value", [
        (b'\x00',          0),           # §1.5.1: decode 0x00 → 0
        (b'\x7f',          127),         # §1.5.1: decode 0x7F → 127
        (b'\x80\x01',      128),         # §1.5.1: decode 0x80 0x01 → 128
        (b'\xff\x7f',      16383),       # §1.5.1: decode 0xFF 0x7F → 16,383
        (b'\x80\x80\x01',  16384),       # §1.5.1: decode 0x80 0x80 0x01 → 16,384
        (b'\xff\xff\x7f',  2097151),     # §1.5.1: decode 0xFF 0xFF 0x7F → 2,097,151
        (b'\x80\x80\x80\x01', 2097152),  # §1.5.1: decode → 2,097,152
        (b'\xff\xff\xff\x7f', 268435455),# §1.5.1: decode → 268,435,455
    ])
    def test_decode_variable_byte_integer(self, encoded_bytes, expected_value):
        """§1.5.1 — Decode Variable Byte Integer bytes to integer value."""
        from blackbull.mqtt.messages import decode_variable_byte_integer
        value, consumed = decode_variable_byte_integer(encoded_bytes)
        assert value == expected_value
        assert consumed == len(encoded_bytes)

    def test_roundtrip_variable_byte_integer(self):
        """§1.5.1 — Round-trip: encode then decode preserves value."""
        from blackbull.mqtt.messages import (
            encode_variable_byte_integer, decode_variable_byte_integer,
        )
        for val in (0, 1, 100, 127, 128, 1000, 16383, 16384, 65535, 100000,
                     2097151, 2097152, 10000000, 268435455):
            encoded = encode_variable_byte_integer(val)
            decoded, consumed = decode_variable_byte_integer(encoded)
            assert decoded == val, f"Round-trip failed for {val}"
            assert consumed == len(encoded)

    def test_decode_variable_byte_integer_with_trailing_data(self):
        """§1.5.1 — Decode only consumes the variable-length prefix;
        trailing bytes are ignored by the decoder (caller tracks them)."""
        from blackbull.mqtt.messages import decode_variable_byte_integer
        value, consumed = decode_variable_byte_integer(b'\x64\xFF\xFF')
        assert value == 100        # 0x64 = 100
        assert consumed == 1       # only the first byte consumed


# ============================================================================
# §2.1.1 — Fixed Header (Control Packet Type + Flags + Remaining Length)
# ============================================================================

class TestFixedHeader:
    """MQTT 5.0 §2.1.1 — Fixed Header parsing.

    Every MQTT Control Packet has a Fixed Header:
      - Byte 1:  Control Packet Type (bits 7-4) + Flags (bits 3-0)
      - Bytes 2-n: Remaining Length (Variable Byte Integer)
    """

    # §2.1.1 Table 2-1 — Control Packet Types
    @pytest.mark.parametrize("packet_type,expected_name", [
        (MQTT_PACKET_CONNECT,     'CONNECT'),
        (MQTT_PACKET_CONNACK,     'CONNACK'),
        (MQTT_PACKET_PUBLISH,     'PUBLISH'),
        (MQTT_PACKET_PUBACK,      'PUBACK'),
        (MQTT_PACKET_PUBREC,      'PUBREC'),
        (MQTT_PACKET_PUBREL,      'PUBREL'),
        (MQTT_PACKET_PUBCOMP,     'PUBCOMP'),
        (MQTT_PACKET_SUBSCRIBE,   'SUBSCRIBE'),
        (MQTT_PACKET_SUBACK,      'SUBACK'),
        (MQTT_PACKET_UNSUBSCRIBE, 'UNSUBSCRIBE'),
        (MQTT_PACKET_UNSUBACK,    'UNSUBACK'),
        (MQTT_PACKET_PINGREQ,     'PINGREQ'),
        (MQTT_PACKET_PINGRESP,    'PINGRESP'),
        (MQTT_PACKET_DISCONNECT,  'DISCONNECT'),
        (MQTT_PACKET_AUTH,        'AUTH'),
    ])
    def test_packet_type_from_name(self, packet_type, expected_name):
        """§2.1.1 Table 2-1 — All 15 control packet types are recognized."""
        from blackbull.mqtt.messages import MQTTPacketType
        assert MQTTPacketType(packet_type).name == expected_name

    # §2.1.1 — Control Packet Type occupies bits 7-4 of byte 1
    @pytest.mark.parametrize("raw_byte,expected_type", [
        (0x10, MQTT_PACKET_CONNECT),     # CONNECT: type=1, flags=0
        (0x20, MQTT_PACKET_CONNACK),     # CONNACK: type=2, flags=0
        (0x30, MQTT_PACKET_PUBLISH),     # PUBLISH: type=3, flags=0
        (0x3D, MQTT_PACKET_PUBLISH),     # PUBLISH: type=3, flags=0xD (all PUBLISH flags)
        (0x40, MQTT_PACKET_PUBACK),      # PUBACK: type=4, flags=0
        (0x50, MQTT_PACKET_PUBREC),      # PUBREC: type=5, flags=0
        (0x62, MQTT_PACKET_PUBREL),      # PUBREL: type=6, flags=2 (fixed)
        (0x70, MQTT_PACKET_PUBCOMP),     # PUBCOMP: type=7, flags=0
        (0x82, MQTT_PACKET_SUBSCRIBE),   # SUBSCRIBE: type=8, flags=2 (fixed)
        (0x90, MQTT_PACKET_SUBACK),      # SUBACK: type=9, flags=0
        (0xA2, MQTT_PACKET_UNSUBSCRIBE), # UNSUBSCRIBE: type=10, flags=2 (fixed)
        (0xB0, MQTT_PACKET_UNSUBACK),    # UNSUBACK: type=11, flags=0
        (0xC0, MQTT_PACKET_PINGREQ),     # PINGREQ: type=12, flags=0
        (0xD0, MQTT_PACKET_PINGRESP),    # PINGRESP: type=13, flags=0
        (0xE0, MQTT_PACKET_DISCONNECT),  # DISCONNECT: type=14, flags=0
        (0xF0, MQTT_PACKET_AUTH),        # AUTH: type=15, flags=0
    ])
    def test_extract_packet_type_from_byte1(self, raw_byte, expected_type):
        """§2.1.1 — Packet type extracted from bits 7-4 of the first byte."""
        from blackbull.mqtt.messages import extract_packet_type
        assert extract_packet_type(raw_byte) == expected_type

    # §2.1.1 — Flags occupy bits 3-0 of byte 1
    @pytest.mark.parametrize("raw_byte,expected_flags", [
        (0x10, 0x0),  # CONNECT: no flags
        (0x30, 0x0),  # PUBLISH: DUP=0, QoS=0, RETAIN=0
        (0x38, 0x8),  # PUBLISH: DUP=0, QoS=1 (0x2<<1=0x4), RETAIN=0 → flags=4... hmm
        (0x3D, 0xD),  # PUBLISH: DUP=1, QoS=2, RETAIN=1 → bits 3-0 = 1101 = 0xD
    ])
    def test_extract_flags_from_byte1(self, raw_byte, expected_flags):
        """§2.1.1 — Flags occupy bits 3-0 of the first byte."""
        from blackbull.mqtt.messages import extract_flags
        assert extract_flags(raw_byte) == expected_flags

    # §3.2.2.1 — PUBLISH flags: DUP (bit 3), QoS (bits 2-1), RETAIN (bit 0)
    @pytest.mark.parametrize("flags_byte,qos,dup,retain", [
        (0x0, 0, False, False),  # QoS 0, no DUP, no RETAIN
        (0x2, 1, False, False),  # QoS 1, no DUP, no RETAIN  (bits: 0010)
        (0x4, 2, False, False),  # QoS 2, no DUP, no RETAIN  (bits: 0100)
        (0x1, 0, False, True),   # QoS 0, no DUP, RETAIN     (bits: 0001)
        (0x8, 0, True,  False),  # QoS 0, DUP, no RETAIN     (bits: 1000)
    ])
    def test_publish_flags_decode(self, flags_byte, qos, dup, retain):
        """§3.2.2.1 — Decode PUBLISH flags into QoS, DUP, RETAIN."""
        from blackbull.mqtt.messages import decode_publish_flags
        result = decode_publish_flags(flags_byte)
        assert result.qos == qos
        assert result.dup == dup
        assert result.retain == retain


# ============================================================================
# §2.2.2 — Properties (MQTT 5.0 extension mechanism)
# ============================================================================

class TestPropertiesEncoding:
    """MQTT 5.0 §2.2.2 — Properties encoding.

    Properties consist of a Property Length (Variable Byte Integer) followed
    by zero or more Properties.  Each Property is a 1-byte Identifier followed
    by a type-specific value.

    §2.2.2.2 Table 2-3 lists all property identifiers.
    """

    def test_encode_empty_properties(self):
        """§2.2.2.1 — Empty properties encode as a single zero byte (Property Length = 0)."""
        from blackbull.mqtt.messages import encode_properties
        assert encode_properties({}) == b'\x00'

    def test_decode_empty_properties(self):
        """§2.2.2.1 — A zero Property Length decodes to an empty dict."""
        from blackbull.mqtt.messages import decode_properties
        props, consumed = decode_properties(b'\x00')
        assert props == {}
        assert consumed == 1  # just the length byte

    @pytest.mark.parametrize("prop_id,prop_name,value", [
        # §2.2.2.2 Table 2-3 — Byte properties
        (PROP_PAYLOAD_FORMAT_INDICATOR, 'payload_format_indicator', 1),
        (PROP_REQUEST_PROBLEM_INFORMATION, 'request_problem_information', 1),
        (PROP_REQUEST_RESPONSE_INFORMATION, 'request_response_information', 0),
        (PROP_MAXIMUM_QOS, 'maximum_qos', 2),
        (PROP_RETAIN_AVAILABLE, 'retain_available', 1),
        (PROP_WILDCARD_SUBSCRIPTION_AVAILABLE, 'wildcard_subscription_available', 1),
        (PROP_SUBSCRIPTION_IDENTIFIER_AVAILABLE, 'subscription_identifier_available', 1),
        (PROP_SHARED_SUBSCRIPTION_AVAILABLE, 'shared_subscription_available', 1),
    ])
    def test_encode_decode_byte_property(self, prop_id, prop_name, value):
        """§2.2.2.2 Table 2-3 — Single-byte properties round-trip."""
        from blackbull.mqtt.messages import encode_properties, decode_properties
        props = {prop_name: value}
        encoded = encode_properties(props)
        decoded, consumed = decode_properties(encoded)
        assert decoded == props

    @pytest.mark.parametrize("prop_id,prop_name,value", [
        # §2.2.2.2 Table 2-3 — Four-Byte Integer properties
        (PROP_MESSAGE_EXPIRY_INTERVAL, 'message_expiry_interval', 3600),
        (PROP_SESSION_EXPIRY_INTERVAL, 'session_expiry_interval', 86400),
        (PROP_WILL_DELAY_INTERVAL, 'will_delay_interval', 30),
        (PROP_MAXIMUM_PACKET_SIZE, 'maximum_packet_size', 268435455),
        (PROP_TOPIC_ALIAS_MAXIMUM, 'topic_alias_maximum', 16),
        (PROP_SERVER_KEEP_ALIVE, 'server_keep_alive', 60),
        (PROP_RECEIVE_MAXIMUM, 'receive_maximum', 65535),
    ])
    def test_encode_decode_four_byte_integer_property(self, prop_id, prop_name, value):
        """§2.2.2.2 Table 2-3 — Four-Byte Integer properties round-trip."""
        from blackbull.mqtt.messages import encode_properties, decode_properties
        props = {prop_name: value}
        encoded = encode_properties(props)
        decoded, consumed = decode_properties(encoded)
        assert decoded == props

    @pytest.mark.parametrize("prop_id,prop_name,value", [
        # §2.2.2.2 Table 2-3 — UTF-8 String properties
        (PROP_CONTENT_TYPE, 'content_type', 'application/json'),
        (PROP_RESPONSE_TOPIC, 'response_topic', 'replies/client1'),
        (PROP_ASSIGNED_CLIENT_IDENTIFIER, 'assigned_client_identifier', 'auto-abc123'),
        (PROP_AUTHENTICATION_METHOD, 'authentication_method', 'SCRAM-SHA-256'),
        (PROP_RESPONSE_INFORMATION, 'response_information', 'broker1.example.com'),
        (PROP_SERVER_REFERENCE, 'server_reference', 'node-2'),
        (PROP_REASON_STRING, 'reason_string', 'Not authorized'),
    ])
    def test_encode_decode_utf8_string_property(self, prop_id, prop_name, value):
        """§2.2.2.2 Table 2-3 — UTF-8 Encoded String properties round-trip."""
        from blackbull.mqtt.messages import encode_properties, decode_properties
        props = {prop_name: value}
        encoded = encode_properties(props)
        decoded, consumed = decode_properties(encoded)
        assert decoded == props

    @pytest.mark.parametrize("prop_id,prop_name,value", [
        # §2.2.2.2 Table 2-3 — Binary Data properties
        (PROP_CORRELATION_DATA, 'correlation_data', b'\x01\x02\x03\x04'),
        (PROP_AUTHENTICATION_DATA, 'authentication_data', b'\xde\xad\xbe\xef'),
    ])
    def test_encode_decode_binary_data_property(self, prop_id, prop_name, value):
        """§2.2.2.2 Table 2-3 — Binary Data properties round-trip."""
        from blackbull.mqtt.messages import encode_properties, decode_properties
        props = {prop_name: value}
        encoded = encode_properties(props)
        decoded, consumed = decode_properties(encoded)
        assert decoded == props

    def test_encode_decode_user_property_pairs(self):
        """§2.2.2.2 Table 2-3 — User Property (0x26) as key-value string pairs."""
        from blackbull.mqtt.messages import encode_properties, decode_properties
        props = {'user_properties': [('key1', 'val1'), ('key2', 'val2')]}
        encoded = encode_properties(props)
        decoded, consumed = decode_properties(encoded)
        assert decoded == props

    def test_encode_decode_multiple_properties(self):
        """§2.2.2 — Multiple different properties in a single Properties block."""
        from blackbull.mqtt.messages import encode_properties, decode_properties
        props = {
            'session_expiry_interval': 3600,
            'receive_maximum': 100,
            'maximum_packet_size': 65536,
            'user_properties': [('app', 'mqtt-test')],
        }
        encoded = encode_properties(props)
        decoded, consumed = decode_properties(encoded)
        assert decoded == props


# ============================================================================
# §2.1.3 / §2.1.2 — Complete packet encode/decode (Fixed + Variable + Payload)
# ============================================================================

class TestPacketRoundTrip:
    """MQTT 5.0 — Full packet serialization round-trip tests.

    These verify that individual MQTT control packets can be constructed
    (via dataclass), serialized to wire format, parsed back, and produce
    an equivalent dataclass.
    """

    # §3.1 — CONNECT packet
    def test_connect_packet_round_trip(self):
        """§3.1.2 — CONNECT packet: encode → decode yields equivalent dataclass."""
        from blackbull.mqtt.messages import (
            MQTTConnect, encode_packet, decode_packet,
        )
        original = MQTTConnect(
            client_id='test-client-001',
            clean_start=True,
            keep_alive=60,
            properties={'session_expiry_interval': 3600},
        )
        wire = encode_packet(original)
        decoded = decode_packet(wire)
        assert isinstance(decoded, MQTTConnect)
        assert decoded.client_id == original.client_id
        assert decoded.clean_start == original.clean_start
        assert decoded.keep_alive == original.keep_alive
        assert decoded.properties == original.properties

    # §3.2 — CONNACK packet
    def test_connack_packet_round_trip(self):
        """§3.2.2 — CONNACK packet: encode → decode yields equivalent dataclass."""
        from blackbull.mqtt.messages import (
            MQTTConnack, encode_packet, decode_packet,
        )
        original = MQTTConnack(
            session_present=False,
            reason_code=0,  # Success
            properties={'server_keep_alive': 120, 'receive_maximum': 100},
        )
        wire = encode_packet(original)
        decoded = decode_packet(wire)
        assert isinstance(decoded, MQTTConnack)
        assert decoded.session_present == original.session_present
        assert decoded.reason_code == original.reason_code
        assert decoded.properties == original.properties

    # §3.3 — PUBLISH packet (QoS 0)
    def test_publish_qos0_packet_round_trip(self):
        """§3.3.2 — PUBLISH QoS 0: no packet identifier, simplest form."""
        from blackbull.mqtt.messages import (
            MQTTPublish, encode_packet, decode_packet,
        )
        original = MQTTPublish(
            topic='sensors/temperature',
            payload=b'23.5',
            qos=0,
            retain=False,
        )
        wire = encode_packet(original)
        decoded = decode_packet(wire)
        assert isinstance(decoded, MQTTPublish)
        assert decoded.topic == original.topic
        assert decoded.payload == original.payload
        assert decoded.qos == 0
        assert decoded.packet_id is None  # §3.3.2-1: no Packet Identifier for QoS 0

    # §3.3 — PUBLISH packet (QoS 1)
    def test_publish_qos1_packet_round_trip(self):
        """§3.3.2 — PUBLISH QoS 1: includes Packet Identifier."""
        from blackbull.mqtt.messages import (
            MQTTPublish, encode_packet, decode_packet,
        )
        original = MQTTPublish(
            topic='sensors/pressure',
            payload=b'1013.25',
            qos=1,
            packet_id=42,
        )
        wire = encode_packet(original)
        decoded = decode_packet(wire)
        assert isinstance(decoded, MQTTPublish)
        assert decoded.qos == 1
        assert decoded.packet_id == 42

    # §3.3 — PUBLISH packet (QoS 2)
    def test_publish_qos2_packet_round_trip(self):
        """§3.3.2 — PUBLISH QoS 2: includes Packet Identifier."""
        from blackbull.mqtt.messages import (
            MQTTPublish, encode_packet, decode_packet,
        )
        original = MQTTPublish(
            topic='alerts/critical',
            payload=b'TEMPERATURE EXCEEDS LIMIT',
            qos=2,
            packet_id=99,
        )
        wire = encode_packet(original)
        decoded = decode_packet(wire)
        assert isinstance(decoded, MQTTPublish)
        assert decoded.qos == 2
        assert decoded.packet_id == 99

    # §3.4 — PUBACK packet
    def test_puback_packet_round_trip(self):
        """§3.4.2 — PUBACK: QoS 1 acknowledgment."""
        from blackbull.mqtt.messages import (
            MQTTPuback, encode_packet, decode_packet,
        )
        original = MQTTPuback(packet_id=42, reason_code=0)
        wire = encode_packet(original)
        decoded = decode_packet(wire)
        assert isinstance(decoded, MQTTPuback)
        assert decoded.packet_id == 42
        assert decoded.reason_code == 0

    # §3.5 — PUBREC packet (QoS 2 step 1)
    def test_pubrec_packet_round_trip(self):
        """§3.5.2 — PUBREC: first acknowledgment in QoS 2 handshake."""
        from blackbull.mqtt.messages import (
            MQTTPubrec, encode_packet, decode_packet,
        )
        original = MQTTPubrec(packet_id=99, reason_code=0)
        wire = encode_packet(original)
        decoded = decode_packet(wire)
        assert isinstance(decoded, MQTTPubrec)
        assert decoded.packet_id == 99

    # §3.6 — PUBREL packet (QoS 2 step 2)
    def test_pubrel_packet_round_trip(self):
        """§3.6.2 — PUBREL: second step in QoS 2 handshake.

        §3.6.1: PUBREL fixed header bits 3-0 MUST be 0b0010 (0x2).
        """
        from blackbull.mqtt.messages import (
            MQTTPubrel, encode_packet, decode_packet,
        )
        original = MQTTPubrel(packet_id=99, reason_code=0)
        wire = encode_packet(original)
        decoded = decode_packet(wire)
        assert isinstance(decoded, MQTTPubrel)
        assert decoded.packet_id == 99
        # §3.6.1 — Fixed header flags must be 0x2
        assert (wire[0] & 0x0F) == 0x02

    # §3.7 — PUBCOMP packet (QoS 2 step 3)
    def test_pubcomp_packet_round_trip(self):
        """§3.7.2 — PUBCOMP: final acknowledgment in QoS 2 handshake."""
        from blackbull.mqtt.messages import (
            MQTTPubcomp, encode_packet, decode_packet,
        )
        original = MQTTPubcomp(packet_id=99, reason_code=0)
        wire = encode_packet(original)
        decoded = decode_packet(wire)
        assert isinstance(decoded, MQTTPubcomp)
        assert decoded.packet_id == 99

    # §3.8 — SUBSCRIBE packet
    def test_subscribe_packet_round_trip(self):
        """§3.8.2 — SUBSCRIBE: topic filter subscriptions.

        §3.8.1: SUBSCRIBE fixed header bits 3-0 MUST be 0b0010 (0x2).
        """
        from blackbull.mqtt.messages import (
            MQTTSubscribe, encode_packet, decode_packet,
        )
        original = MQTTSubscribe(
            packet_id=1,
            subscriptions=[('sensors/+/temperature', 1), ('alerts/#', 2)],
        )
        wire = encode_packet(original)
        decoded = decode_packet(wire)
        assert isinstance(decoded, MQTTSubscribe)
        assert decoded.packet_id == 1
        assert len(decoded.subscriptions) == 2
        assert ('sensors/+/temperature', 1) in decoded.subscriptions
        assert ('alerts/#', 2) in decoded.subscriptions

    # §3.9 — SUBACK packet
    def test_suback_packet_round_trip(self):
        """§3.9.2 — SUBACK: subscription acknowledgment with reason codes."""
        from blackbull.mqtt.messages import (
            MQTTSuback, encode_packet, decode_packet,
        )
        original = MQTTSuback(
            packet_id=1,
            reason_codes=[0, 0x80],  # Granted QoS 0, Unspecified error
        )
        wire = encode_packet(original)
        decoded = decode_packet(wire)
        assert isinstance(decoded, MQTTSuback)
        assert decoded.packet_id == 1
        assert decoded.reason_codes == [0, 0x80]

    # §3.10 — UNSUBSCRIBE packet
    def test_unsubscribe_packet_round_trip(self):
        """§3.10.2 — UNSUBSCRIBE: unsubscribe from topics.

        §3.10.1: UNSUBSCRIBE fixed header bits 3-0 MUST be 0b0010 (0x2).
        """
        from blackbull.mqtt.messages import (
            MQTTUnsubscribe, encode_packet, decode_packet,
        )
        original = MQTTUnsubscribe(
            packet_id=2,
            topics=['sensors/+/temperature'],
        )
        wire = encode_packet(original)
        decoded = decode_packet(wire)
        assert isinstance(decoded, MQTTUnsubscribe)
        assert decoded.packet_id == 2
        assert decoded.topics == ['sensors/+/temperature']

    # §3.11 — UNSUBACK packet
    def test_unsuback_packet_round_trip(self):
        """§3.11.2 — UNSUBACK: unsubscribe acknowledgment."""
        from blackbull.mqtt.messages import (
            MQTTUnsuback, encode_packet, decode_packet,
        )
        original = MQTTUnsuback(packet_id=2, reason_codes=[0])
        wire = encode_packet(original)
        decoded = decode_packet(wire)
        assert isinstance(decoded, MQTTUnsuback)
        assert decoded.packet_id == 2

    # §3.12 — PINGREQ packet
    def test_pingreq_packet_round_trip(self):
        """§3.12 — PINGREQ: keep-alive heartbeat request.

        PINGREQ has no variable header and no payload.
        Fixed header: 0xC0, Remaining Length: 0.
        """
        from blackbull.mqtt.messages import (
            MQTTPingreq, encode_packet, decode_packet,
        )
        original = MQTTPingreq()
        wire = encode_packet(original)
        assert wire == b'\xC0\x00'  # §3.12 — exact wire representation
        decoded = decode_packet(wire)
        assert isinstance(decoded, MQTTPingreq)

    # §3.13 — PINGRESP packet
    def test_pingresp_packet_round_trip(self):
        """§3.13 — PINGRESP: keep-alive heartbeat response.

        PINGRESP has no variable header and no payload.
        Fixed header: 0xD0, Remaining Length: 0.
        """
        from blackbull.mqtt.messages import (
            MQTTPingresp, encode_packet, decode_packet,
        )
        original = MQTTPingresp()
        wire = encode_packet(original)
        assert wire == b'\xD0\x00'  # §3.13 — exact wire representation
        decoded = decode_packet(wire)
        assert isinstance(decoded, MQTTPingresp)

    # §3.14 — DISCONNECT packet
    def test_disconnect_packet_round_trip(self):
        """§3.14.2 — DISCONNECT: graceful disconnect with optional reason code."""
        from blackbull.mqtt.messages import (
            MQTTDisconnect, encode_packet, decode_packet,
        )
        original = MQTTDisconnect(reason_code=0)
        wire = encode_packet(original)
        decoded = decode_packet(wire)
        assert isinstance(decoded, MQTTDisconnect)
        assert decoded.reason_code == 0

    # §3.15 — AUTH packet (MQTT 5.0 only)
    def test_auth_packet_round_trip(self):
        """§3.15.2 — AUTH: extended authentication exchange.

        §3.15.1: AUTH fixed header bits 3-0 MUST be 0b0000 (0x0).
        """
        from blackbull.mqtt.messages import (
            MQTTAuth, encode_packet, decode_packet,
        )
        original = MQTTAuth(
            reason_code=0x18,  # Continue authentication
            properties={'authentication_method': 'SCRAM-SHA-256',
                        'authentication_data': b'client-proof'},
        )
        wire = encode_packet(original)
        decoded = decode_packet(wire)
        assert isinstance(decoded, MQTTAuth)
        assert decoded.reason_code == 0x18
        assert decoded.properties['authentication_method'] == 'SCRAM-SHA-256'


# ============================================================================
# §3.1 — CONNECT packet fixed header validation
# ============================================================================

class TestConnectPacketValidation:
    """MQTT 5.0 §3.1 — CONNECT packet validation rules.

    The CONNECT packet has strict rules about protocol name, protocol level,
    and flags that distinguish MQTT 5.0 from earlier versions.
    """

    def test_connect_protocol_name_is_mqtt(self):
        """§3.1.2.1 — Protocol Name MUST be 'MQTT' (UTF-8 encoded)."""
        from blackbull.mqtt.messages import MQTTConnect, encode_packet
        connect = MQTTConnect(client_id='test', clean_start=True, keep_alive=60)
        wire = encode_packet(connect)
        # §3.1.2.1: Protocol Name is a UTF-8 string "MQTT"
        # UTF-8 string: 2-byte length prefix + data
        proto_name_len = int.from_bytes(wire[2:4], 'big')
        proto_name = wire[4:4 + proto_name_len].decode('utf-8')
        assert proto_name == 'MQTT'

    def test_connect_protocol_level_is_5(self):
        """§3.1.2.2 — Protocol Level MUST be 5 for MQTT 5.0."""
        from blackbull.mqtt.messages import MQTTConnect, encode_packet
        connect = MQTTConnect(client_id='test', clean_start=True, keep_alive=60)
        wire = encode_packet(connect)
        # §3.1.2.2: Protocol Level is a single byte following the Protocol Name
        # After fixed header (2+ bytes) + protocol name (2+4=6 bytes), level is at offset
        # Fixed header: type/flags(1) + remaining_len(variable). For minimal connect:
        # byte 0: 0x10, byte 1: remaining_len
        remaining_len_consumed = 1
        if wire[1] & 0x80:  # multi-byte remaining length
            remaining_len_consumed = 2
        level_offset = 1 + remaining_len_consumed + 2 + 4  # hdr + len_prefix + 'MQTT'
        assert wire[level_offset] == 5  # Protocol Level == 5

    def test_connect_clean_start_flag(self):
        """§3.1.2.3 — Clean Start flag in Connect Flags (bit 1 of byte 0 of flags)."""
        from blackbull.mqtt.messages import MQTTConnect, encode_packet
        # Clean Start = True
        connect_clean = MQTTConnect(client_id='test', clean_start=True, keep_alive=60)
        wire_clean = encode_packet(connect_clean)
        # The Connect Flags byte follows the Protocol Level byte
        # offset: fixed_hdr(1+RL) + proto_name_len(2) + 'MQTT'(4) + proto_level(1)
        remaining_len_consumed = 1 if not (wire_clean[1] & 0x80) else 2
        flags_offset = 1 + remaining_len_consumed + 2 + 4 + 1
        assert wire_clean[flags_offset] & 0x02  # Clean Start bit set

        # Clean Start = False
        connect_not_clean = MQTTConnect(client_id='test', clean_start=False, keep_alive=60)
        wire_not_clean = encode_packet(connect_not_clean)
        flags_off = 1 + (1 if not (wire_not_clean[1] & 0x80) else 2) + 2 + 4 + 1
        assert not (wire_not_clean[flags_off] & 0x02)  # Clean Start bit NOT set

    def test_connect_keep_alive_field(self):
        """§3.1.2.10 — Keep Alive is a 2-byte unsigned integer (seconds)."""
        from blackbull.mqtt.messages import MQTTConnect, encode_packet
        for ka in (0, 60, 300, 65535):
            connect = MQTTConnect(client_id='test', clean_start=True, keep_alive=ka)
            wire = encode_packet(connect)
            # Keep Alive is the last 2 bytes before properties + client_id
            # We can verify by decoding the packet back
            from blackbull.mqtt.messages import decode_packet
            decoded = decode_packet(wire)
            assert decoded.keep_alive == ka


# ============================================================================
# §4 — Reason Codes (MQTT 5.0 standardized reason codes)
# ============================================================================

class TestReasonCodes:
    """MQTT 5.0 — Reason Codes (§3.x for each packet type).

    MQTT 5.0 standardizes reason codes across all acknowledgment packets.
    §3.2.2.2 (CONNACK), §3.4.2.1 (PUBACK), §3.5.2.1 (PUBREC),
    §3.6.2.1 (PUBREL), §3.7.2.1 (PUBCOMP), §3.9.2.1 (SUBACK),
    §3.11.2.1 (UNSUBACK), §3.14.2.1 (DISCONNECT), §3.15.2.1 (AUTH).
    """

    # Reason-code byte values follow MQTT 5.0 OASIS §2.4 Table 2-9 exactly
    # (the high error range is sparse: 0x8C, then 0x8D..0xA2).  0x00 has the
    # canonical name 'Success'; in a DISCONNECT it is also read as "Normal
    # disconnection", but the code's single canonical name is 'Success'.
    @pytest.mark.parametrize("code,expected_name", [
        (0x00, 'Success'),
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
    ])
    def test_reason_code_recognized(self, code, expected_name):
        """All MQTT 5.0 reason codes are recognized by name."""
        from blackbull.mqtt.messages import MQTTReasonCode
        rc = MQTTReasonCode(code)
        assert rc.name == expected_name

    def test_reason_code_less_than_0x80_is_success(self):
        """§3.x — Reason codes < 0x80 indicate success/normal."""
        from blackbull.mqtt.messages import MQTTReasonCode
        for code in (0x00, 0x01, 0x04, 0x10, 0x11, 0x18, 0x19):
            rc = MQTTReasonCode(code)
            assert rc.is_success, f"Code 0x{code:02X} should be success"

    def test_reason_code_0x80_or_above_is_error(self):
        """§3.x — Reason codes >= 0x80 indicate error."""
        from blackbull.mqtt.messages import MQTTReasonCode
        for code in (0x80, 0x81, 0x82, 0x8F, 0x99):
            rc = MQTTReasonCode(code)
            assert rc.is_error, f"Code 0x{code:02X} should be error"
