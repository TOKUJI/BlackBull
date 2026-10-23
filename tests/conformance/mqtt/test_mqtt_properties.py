"""§2.2.2.2 Table 2-3 — the 27 MQTT 5.0 property identifiers, transcribed
from the OASIS standard rather than derived from the implementation.
"""

import pytest


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

    # A value at the top of each Table 2-3 data type's range, and its octets.
    SAMPLES = {
        'Byte': (0xFF, 'ff'),
        'Two Byte Integer': (0xFFFF, 'ffff'),
        'Four Byte Integer': (0xFFFFFFFF, 'ffffffff'),
        'Variable Byte Integer': (268435455, 'ffffff7f'),
        'UTF-8 String': ('é', '0002c3a9'),
        'Binary Data': (b'\x00', '000100'),
        'UTF-8 String Pair': ([('k', 'v')], '00016b000176'),
    }

    def test_all_27_property_identifiers_known(self):
        """§2.2.2.2 Table 2-3 — There are 27 defined property identifiers."""
        from blackbull.mqtt.messages import PropertyId
        assert {int(p) for p in PropertyId} == {row[0] for row in self.ALL_PROPERTIES}

    @pytest.mark.parametrize("prop_id,prop_name,type_cat,pkt_types", ALL_PROPERTIES)
    def test_property_identifier_recognized(self, prop_id, prop_name, type_cat, pkt_types):
        """Each Table 2-3 property is named, typed and round-trips on the wire."""
        from blackbull.mqtt.messages import PropertyId, decode_properties, encode_properties
        assert PropertyId(prop_id).name.lower() == prop_name
        key = 'user_properties' if prop_name == 'user_property' else prop_name
        value, octets = self.SAMPLES[type_cat]
        wire = bytes.fromhex(f'{prop_id:02x}{octets}')
        assert encode_properties({key: value}) == bytes([len(wire)]) + wire
        assert decode_properties(bytes([len(wire)]) + wire) == ({key: value}, len(wire) + 1)
