"""§2.2.2 — a property value stays inside the Property Length that declares it.

The property section is a nested range: its decoders may consume only bytes
inside the declared end.  A decoder that reaches past it re-reads payload (or
the next structure) as property value and counts it twice — the packet then
looks well-formed and the broker acts on bytes that were never properties.
A section short of its own value is malformed, not a short read: the packet
is complete by Remaining Length, so nothing may wait for more bytes.
"""

from __future__ import annotations

import pytest

from blackbull.mqtt.connection import PacketFramer
from blackbull.mqtt.messages import (
    MQTTDecodeError,
    IncompletePacket,
    decode_packet,
    decode_properties,
    encode_properties,
)


EXPIRY_42 = bytes([0x02]) + (42).to_bytes(4, 'big')     # message_expiry_interval


def _section(props_bytes: bytes, declared: int | None = None) -> bytes:
    """Property section: VBI length prefix + bytes, optionally lying."""
    n = len(props_bytes) if declared is None else declared
    assert n < 128
    return bytes([n]) + props_bytes


class TestPropertyValuesStayInsideTheSection:
    def test_a_byte_value_byte_past_the_end_is_malformed(self):
        data = _section(bytes([0x01]), declared=1) + b'\x00'   # payload-format
        with pytest.raises(MQTTDecodeError):
            decode_properties(data)

    def test_a_uint16_cut_by_the_end_is_malformed(self):
        data = _section(bytes([0x22, 0x00]), declared=2) + b'\xff\xff'  # receive-max
        with pytest.raises(MQTTDecodeError):
            decode_properties(data)

    def test_a_uint32_cut_by_the_end_is_malformed(self):
        data = _section(bytes([0x02, 0x00, 0x00]), declared=3) + b'\x00\x00\x00'
        with pytest.raises(MQTTDecodeError):
            decode_properties(data)

    def test_a_vbi_cut_by_the_end_is_malformed_not_incomplete(self):
        data = _section(bytes([0x0B, 0x80]), declared=2) + b'\x01'  # sub-identifier
        with pytest.raises(MQTTDecodeError) as excinfo:
            decode_properties(data)
        assert not isinstance(excinfo.value, IncompletePacket)

    def test_a_utf8_length_prefix_cut_by_the_end_is_malformed(self):
        data = _section(bytes([0x08, 0x00]), declared=2) + b'\x00\x05topic'
        with pytest.raises(MQTTDecodeError):
            decode_properties(data)

    def test_a_utf8_body_past_the_end_is_malformed(self):
        data = _section(bytes([0x08, 0x00, 0x05]) + b'to', declared=3) + b'pic'
        with pytest.raises(MQTTDecodeError):
            decode_properties(data)

    def test_a_binary_body_past_the_end_is_malformed(self):
        data = _section(bytes([0x09, 0x00, 0x04, 0x01]), declared=3) + b'\x02\x03\x04'
        with pytest.raises(MQTTDecodeError):
            decode_properties(data)

    def test_a_user_property_key_past_the_end_is_malformed(self):
        data = _section(bytes([0x26, 0x00, 0x02, 0x61]), declared=3) + b'b'
        with pytest.raises(MQTTDecodeError):
            decode_properties(data)

    def test_a_user_property_value_past_the_end_is_malformed(self):
        data = _section(bytes([0x26, 0x00, 0x61, 0x00]), declared=3) + b'b'
        with pytest.raises(MQTTDecodeError):
            decode_properties(data)

    def test_an_unknown_identifier_inside_the_section_is_malformed(self):
        with pytest.raises(MQTTDecodeError):
            decode_properties(_section(bytes([0xFF])))


class TestSectionBoundariesArePositive:
    def test_a_value_that_ends_exactly_at_the_end_decodes(self):
        data = _section(EXPIRY_42)
        assert decode_properties(data) == (
            {'message_expiry_interval': 42}, len(data))

    def test_empty_properties_decode_to_nothing(self):
        assert decode_properties(_section(b'')) == ({}, 1)

    def test_repeated_user_properties_aggregate_in_order(self):
        raw = encode_properties({'user_properties': [('a', '1'), ('b', '2')]})
        props, consumed = decode_properties(raw)
        assert props == {'user_properties': [('a', '1'), ('b', '2')]}
        assert consumed == len(raw)

    def test_consumption_counts_only_the_section(self):
        section = _section(EXPIRY_42)
        props, consumed = decode_properties(section + b'\x2a')   # payload byte
        assert consumed == len(section)
        assert props == {'message_expiry_interval': 42}


def _publish_packet(declared: int | None) -> bytes:
    expiry = _section(EXPIRY_42, declared=declared)
    body = b'\x00\x03top' + expiry + b'x'          # topic 'top', payload 'x'
    return bytes([0x30]) + bytes([len(body)]) + body


class TestEntryClassification:
    def test_a_shrunk_property_length_does_not_reach_into_the_payload(self):
        with pytest.raises(MQTTDecodeError):
            decode_packet(_publish_packet(declared=1))

    def test_the_control_packet_keeps_expiry_and_payload_intact(self):
        raw = _publish_packet(declared=None)
        msg, consumed = decode_packet(raw)
        assert msg.properties == {'message_expiry_interval': 42}
        assert msg.payload == b'x'
        assert consumed == len(raw)

    def test_a_complete_packet_with_a_short_property_is_malformed_not_incomplete(self):
        with pytest.raises(MQTTDecodeError) as excinfo:
            decode_packet(_publish_packet(declared=1))
        assert not isinstance(excinfo.value, IncompletePacket)

    def test_the_framer_raises_and_keeps_no_partial_packet(self):
        framer = PacketFramer()
        framer.feed(_publish_packet(declared=1))
        with pytest.raises(MQTTDecodeError):
            list(framer)


def _site_packets(label: str, props: bytes) -> bytes:
    """A minimal legal packet of each property-carrying kind."""
    pid = b'\x00\x01'                                 # packet identifier 1
    topic = b'\x00\x03top'
    if label == 'connect':
        body = b'\x00\x04MQTT\x05\x02\x00\x3c' + props + b'\x00\x01c'
        return bytes([0x10]) + bytes([len(body)]) + body
    if label == 'connect-will':
        body = (b'\x00\x04MQTT\x05\x06\x00\x3c' + props
                + b'\x00\x01c' + props + topic + b'\x00\x00')
        return bytes([0x10]) + bytes([len(body)]) + body
    if label == 'publish':
        body = topic + props + b'x'
        return bytes([0x30]) + bytes([len(body)]) + body
    if label == 'puback':
        body = pid + b'\x00' + props                # reason code precedes
        return bytes([0x40]) + bytes([len(body)]) + body
    if label == 'connack':
        body = b'\x00\x00' + props
        return bytes([0x20]) + bytes([len(body)]) + body
    if label == 'disconnect':
        body = b'\x00' + props
        return bytes([0xE0]) + bytes([len(body)]) + body
    if label == 'subscribe':
        body = pid + props + topic + b'\x00'
        return bytes([0x82]) + bytes([len(body)]) + body
    raise AssertionError(label)


class TestEveryPropertySiteIsBounded:
    SITES = ['connect', 'connect-will', 'publish', 'puback', 'connack',
             'disconnect', 'subscribe']

    @pytest.mark.parametrize('label', SITES)
    def test_the_control_packet_decodes(self, label):
        ok = _section(EXPIRY_42)
        msg, _ = decode_packet(_site_packets(label, ok))
        assert msg is not None

    @pytest.mark.parametrize('label', SITES)
    def test_a_value_past_the_section_is_malformed_at_every_site(self, label):
        bad = _section(EXPIRY_42, declared=1)       # identifier only
        with pytest.raises(MQTTDecodeError):
            decode_packet(_site_packets(label, bad))
