"""Per-type MQTT body grammar: minimum length, optional tail, exact consumption.

Remaining Length says how many octets a packet declares, not that those octets
are a whole packet: each type has a minimum body, a set of optional trailing
fields, and a rule for what may follow them.  A decoder that reads a short
slice, ignores a leftover octet, or accepts a zero Packet Identifier turns a
Malformed Packet into a message the broker acts on.

MQTT 5.0 §2.2.1 and §3.1-§3.15.

This is the *outer* body grammar: how many octets each type's body must hold
and what may follow its last field.  Whether a property value stays inside the
Property Length that declares it is the separate §2.2.2 invariant, checked on
its own.
"""
from __future__ import annotations

import pytest

from blackbull.mqtt.connection import PacketFramer
from blackbull.mqtt.messages import (
    MQTTAuth,
    MQTTConnect,
    MQTTConnack,
    MQTTDecodeError,
    MQTTDisconnect,
    MQTTPacketType,
    MQTTPuback,
    MQTTPublish,
    MQTTSuback,
    MQTTSubscribe,
    MQTTUnsuback,
    MQTTUnsubscribe,
    decode_packet,
    encode_packet,
    encode_variable_byte_integer,
)

# §2.1.3 — PUBREL, SUBSCRIBE and UNSUBSCRIBE reserve these fixed-header flags.
_RESERVED_0010 = 0x2


def packet(packet_type: MQTTPacketType, body: bytes = b'', flags: int = 0) -> bytes:
    """Fixed header + Remaining Length + *body*, with no encoder in the way."""
    first = (int(packet_type) << 4) | (flags & 0x0F)
    return bytes([first]) + encode_variable_byte_integer(len(body)) + body


def _utf8(text: bytes) -> bytes:
    return len(text).to_bytes(2, 'big') + text


def _props(props: bytes = b'') -> bytes:
    return encode_variable_byte_integer(len(props)) + props


def _ack_body(packet_id: int = 1, reason: int | None = None,
              props: bytes | None = None, surplus: bytes = b'') -> bytes:
    """Account body: identifier [+ reason [properties]] [+ surplus]."""
    body = packet_id.to_bytes(2, 'big')
    if reason is not None:
        body += bytes([reason])
    if props is not None:
        body += _props(props)
    return body + surplus


def _publish_body(topic: bytes = b'a', packet_id: int | None = None,
                  props: bytes = b'', payload: bytes = b'') -> bytes:
    body = _utf8(topic)
    if packet_id is not None:
        body += packet_id.to_bytes(2, 'big')
    return body + _props(props) + payload


# CONNECT: protocol name, level 5, clean start, keep alive 60, no properties,
# client id "x".
_CONNECT_BODY = (b'\x00\x04MQTT' + b'\x05' + b'\x02' + b'\x00\x3c'
                 + _props() + _utf8(b'x'))
# SUBSCRIBE/UNSUBSCRIBE payload: one filter plus one subscription-options octet.
_ONE_FILTER = _utf8(b'a/b') + b'\x00'

SHORTER_THAN_THE_TYPE_REQUIRES = [
    ('puback-empty', packet(MQTTPacketType.PUBACK)),
    ('puback-one-octet', packet(MQTTPacketType.PUBACK, b'\x00')),
    ('pubrec-empty', packet(MQTTPacketType.PUBREC)),
    ('pubrel-empty', packet(MQTTPacketType.PUBREL, flags=_RESERVED_0010)),
    ('pubcomp-one-octet', packet(MQTTPacketType.PUBCOMP, b'\x01')),
    ('publish-qos1-no-packet-id', packet(
        MQTTPacketType.PUBLISH, _utf8(b'a'), flags=0x2)),
    ('publish-qos1-one-octet-id', packet(
        MQTTPacketType.PUBLISH, _utf8(b'a') + b'\x01', flags=0x2)),
    ('subscribe-no-filter', packet(
        MQTTPacketType.SUBSCRIBE, _ack_body(props=b''), flags=_RESERVED_0010)),
    ('unsubscribe-no-filter', packet(
        MQTTPacketType.UNSUBSCRIBE, _ack_body(props=b''), flags=_RESERVED_0010)),
    ('suback-no-reason-code', packet(MQTTPacketType.SUBACK, _ack_body(props=b''))),
    ('unsuback-no-reason-code', packet(MQTTPacketType.UNSUBACK, _ack_body(props=b''))),
    ('connack-flags-only', packet(MQTTPacketType.CONNACK, b'\x00')),
    ('connack-two-octets', packet(MQTTPacketType.CONNACK, b'\x01\x00')),
    ('connect-client-id-truncated', packet(MQTTPacketType.CONNECT, _CONNECT_BODY[:-1])),
]

ZERO_PACKET_IDENTIFIER = [
    ('puback', packet(MQTTPacketType.PUBACK, _ack_body(packet_id=0))),
    ('pubrec', packet(MQTTPacketType.PUBREC, _ack_body(packet_id=0))),
    ('pubrel', packet(MQTTPacketType.PUBREL, _ack_body(packet_id=0),
                      flags=_RESERVED_0010)),
    ('pubcomp', packet(MQTTPacketType.PUBCOMP, _ack_body(packet_id=0))),
    ('publish-qos1', packet(MQTTPacketType.PUBLISH,
                            _publish_body(topic=b'a', packet_id=0), flags=0x2)),
    ('subscribe', packet(MQTTPacketType.SUBSCRIBE,
                         _ack_body(packet_id=0, props=b'') + _ONE_FILTER,
                         flags=_RESERVED_0010)),
    ('suback', packet(MQTTPacketType.SUBACK,
                      _ack_body(packet_id=0, props=b'') + b'\x00')),
    ('unsubscribe', packet(MQTTPacketType.UNSUBSCRIBE,
                           _ack_body(packet_id=0, props=b'') + _ONE_FILTER,
                           flags=_RESERVED_0010)),
    ('unsuback', packet(MQTTPacketType.UNSUBACK,
                        _ack_body(packet_id=0, props=b'') + b'\x00')),
]

BYTES_AFTER_THE_LAST_FIELD = [
    ('puback-after-properties', packet(
        MQTTPacketType.PUBACK, _ack_body(reason=0, props=b'', surplus=b'\xff'))),
    ('pubrel-after-reason', packet(
        MQTTPacketType.PUBREL, _ack_body(reason=0, props=b'', surplus=b'\x00'),
        flags=_RESERVED_0010)),
    ('connack-after-properties', packet(
        MQTTPacketType.CONNACK, b'\x00\x00' + _props() + b'\xff')),
    ('disconnect-after-properties', packet(
        MQTTPacketType.DISCONNECT, b'\x00' + _props() + b'\xff')),
    ('auth-after-reason', packet(MQTTPacketType.AUTH, b'\x00\x00\xff')),
    ('connect-after-client-id', packet(
        MQTTPacketType.CONNECT, _CONNECT_BODY + b'\xff')),
]

BODY_MUST_BE_ABSENT = [
    ('pingreq-one-octet', packet(MQTTPacketType.PINGREQ, b'\x00')),
    ('pingresp-one-octet', packet(MQTTPacketType.PINGRESP, b'\x00')),
    ('pingreq-two-octets', packet(MQTTPacketType.PINGREQ, b'\x00\x00')),
]


@pytest.mark.parametrize('label,wire', SHORTER_THAN_THE_TYPE_REQUIRES,
                         ids=[case[0] for case in SHORTER_THAN_THE_TYPE_REQUIRES])
def test_a_body_shorter_than_the_type_requires_is_malformed(label, wire):
    with pytest.raises(MQTTDecodeError):
        decode_packet(wire)


@pytest.mark.parametrize('label,wire', ZERO_PACKET_IDENTIFIER,
                         ids=[case[0] for case in ZERO_PACKET_IDENTIFIER])
def test_a_zero_packet_identifier_is_malformed(label, wire):
    """§2.2.1 — a Packet Identifier of 0 is not allowed."""
    with pytest.raises(MQTTDecodeError):
        decode_packet(wire)


@pytest.mark.parametrize('label,wire', BYTES_AFTER_THE_LAST_FIELD,
                         ids=[case[0] for case in BYTES_AFTER_THE_LAST_FIELD])
def test_bytes_after_the_last_field_are_malformed(label, wire):
    with pytest.raises(MQTTDecodeError):
        decode_packet(wire)


@pytest.mark.parametrize('label,wire', BODY_MUST_BE_ABSENT,
                         ids=[case[0] for case in BODY_MUST_BE_ABSENT])
def test_ping_carries_no_body(label, wire):
    """§3.12, §3.13 — PINGREQ and PINGRESP have no payload."""
    with pytest.raises(MQTTDecodeError):
        decode_packet(wire)


# ---------------------------------------------------------------------------
# The legal forms the same rules must keep accepting
# ---------------------------------------------------------------------------

def test_a_shortened_ack_still_decodes():
    """§3.4.2.1 — Remaining Length 2 omits the reason code and properties."""
    message, consumed = decode_packet(packet(MQTTPacketType.PUBACK, b'\x00\x07'))
    assert message == MQTTPuback(packet_id=7, reason_code=0, properties={})
    assert consumed == 4


def test_a_reason_only_ack_still_decodes():
    """Remaining Length 3 carries a reason code and no properties."""
    message, _ = decode_packet(packet(MQTTPacketType.PUBACK, b'\x00\x07\x10'))
    assert message == MQTTPuback(packet_id=7, reason_code=0x10, properties={})


def test_an_ack_with_properties_still_decodes():
    message, _ = decode_packet(packet(
        MQTTPacketType.PUBACK,
        _ack_body(packet_id=7, reason=0, props=b'\x1f\x00\x00')))
    assert message == MQTTPuback(packet_id=7, reason_code=0,
                                 properties={'reason_string': ''})


def test_a_packet_identifier_of_one_is_legal():
    message, _ = decode_packet(packet(MQTTPacketType.PUBACK, b'\x00\x01'))
    assert message.packet_id == 1


def test_empty_and_reason_only_disconnect_and_auth_still_decode():
    assert decode_packet(packet(MQTTPacketType.DISCONNECT))[0] == MQTTDisconnect()
    assert decode_packet(packet(MQTTPacketType.DISCONNECT, b'\x00'))[0] == \
        MQTTDisconnect(reason_code=0)
    assert decode_packet(packet(MQTTPacketType.AUTH))[0] == MQTTAuth()
    assert decode_packet(packet(MQTTPacketType.AUTH, b'\x18'))[0] == \
        MQTTAuth(reason_code=0x18)


def test_a_connack_with_an_empty_properties_block_still_decodes():
    """§3.2.2 — MQTT 5 CONNACK ends with a Property Length, minimum 3 octets."""
    assert decode_packet(packet(MQTTPacketType.CONNACK, b'\x01\x00\x00'))[0] == \
        MQTTConnack(session_present=True, reason_code=0, properties={})


def test_legal_payloads_still_decode():
    subscribe = packet(MQTTPacketType.SUBSCRIBE,
                       _ack_body(props=b'') + _ONE_FILTER,
                       flags=_RESERVED_0010)
    assert decode_packet(subscribe)[0] == MQTTSubscribe(
        packet_id=1, subscriptions=[('a/b', 0)], properties={},
        subscription_options=[{'qos': 0, 'no_local': False,
                               'retain_as_published': False,
                               'retain_handling': 0}])
    unsubscribe = packet(MQTTPacketType.UNSUBSCRIBE,
                         _ack_body(props=b'') + _utf8(b'a/b'),
                         flags=_RESERVED_0010)
    assert decode_packet(unsubscribe)[0] == MQTTUnsubscribe(
        packet_id=1, topics=['a/b'], properties={})
    suback = packet(MQTTPacketType.SUBACK, _ack_body(props=b'') + b'\x01')
    assert decode_packet(suback)[0] == MQTTSuback(
        packet_id=1, reason_codes=[1], properties={})
    unsuback = packet(MQTTPacketType.UNSUBACK, _ack_body(props=b'') + b'\x00')
    assert decode_packet(unsuback)[0] == MQTTUnsuback(
        packet_id=1, reason_codes=[0], properties={})
    publish = packet(MQTTPacketType.PUBLISH,
                     _publish_body(topic=b'a/b', packet_id=9, payload=b'body'),
                     flags=0x2)
    assert decode_packet(publish)[0] == MQTTPublish(
        topic='a/b', payload=b'body', qos=1, packet_id=9)
    assert decode_packet(packet(MQTTPacketType.CONNECT, _CONNECT_BODY))[0] == \
        MQTTConnect(client_id='x', clean_start=True, keep_alive=60,
                    proto_level=5)


def test_a_pre_v5_connect_carries_no_properties_block():
    """§3.1.2.11 — the Properties block exists from MQTT 5 on."""
    message = MQTTConnect(client_id='x', clean_start=True, keep_alive=60,
                          proto_level=4)
    assert decode_packet(encode_packet(message))[0] == message


def test_a_pre_v5_connect_carries_no_will_properties():
    """§3.1.3.2 — and neither does the Will's."""
    message = MQTTConnect(client_id='x', clean_start=True, keep_alive=60,
                          proto_level=4, will_topic='w', will_payload=b'p')
    assert decode_packet(encode_packet(message))[0] == message


def test_a_legal_message_round_trips():
    messages = [
        MQTTConnect(client_id='x', clean_start=True, keep_alive=60),
        MQTTPuback(packet_id=3),
        MQTTSuback(packet_id=3, reason_codes=[0, 1]),
        MQTTUnsuback(packet_id=3, reason_codes=[0]),
        MQTTPublish(topic='a/b', payload=b'x', qos=1, packet_id=4),
        MQTTDisconnect(reason_code=0x8E),
    ]
    for message in messages:
        assert decode_packet(encode_packet(message))[0] == message


def test_a_malformed_packet_never_reaches_the_framer():
    """The entry point the connection actor reads drops it (§4.13)."""
    for _, wire in (SHORTER_THAN_THE_TYPE_REQUIRES + ZERO_PACKET_IDENTIFIER
                    + BYTES_AFTER_THE_LAST_FIELD + BODY_MUST_BE_ABSENT):
        framer = PacketFramer()
        framer.feed(wire)
        assert list(framer) == [], wire.hex()


def test_the_packet_behind_a_malformed_one_still_decodes():
    """Its deficit may not be taken from the next packet's octets."""
    valid = packet(MQTTPacketType.PUBACK, b'\x00\x01')
    for _, wire in (SHORTER_THAN_THE_TYPE_REQUIRES + ZERO_PACKET_IDENTIFIER
                    + BYTES_AFTER_THE_LAST_FIELD + BODY_MUST_BE_ABSENT):
        framer = PacketFramer()
        framer.feed(wire + valid)
        assert list(framer) == [MQTTPuback(packet_id=1)], wire.hex()


def test_a_body_the_message_class_refuses_is_a_decode_error():
    """§3.1.2.9 — PASSWORD without USERNAME is Malformed, like any other."""
    connect = (b'\x00\x04MQTT' + b'\x05' + b'\x40' + b'\x00\x3c'
               + _props() + _utf8(b'x') + _utf8(b'p'))
    with pytest.raises(MQTTDecodeError):
        decode_packet(packet(MQTTPacketType.CONNECT, connect))


def test_two_legal_packets_decode_one_at_a_time():
    first = packet(MQTTPacketType.PUBACK, b'\x00\x01')
    second = packet(MQTTPacketType.PINGREQ)
    message, consumed = decode_packet(first + second)
    assert message == MQTTPuback(packet_id=1)
    assert consumed == len(first)
    assert decode_packet((first + second)[consumed:])[0].packet_type == \
        MQTTPacketType.PINGREQ
    framer = PacketFramer()
    framer.feed(first + second)
    assert [type(m).__name__ for m in framer] == ['MQTTPuback', 'MQTTPingreq']
