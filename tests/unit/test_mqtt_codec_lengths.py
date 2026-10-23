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
    RESERVED_FLAGS_0010,
    ReasonCode,
    MQTTAuth,
    MQTTConnect,
    MQTTConnack,
    MQTTDecodeError,
    IncompletePacket,
    MQTTDisconnect,
    MQTTPacketType,
    MQTTPingreq,
    MQTTPingresp,
    MQTTPuback,
    MQTTPublish,
    MQTTPubcomp,
    MQTTPubrec,
    MQTTPubrel,
    MQTTSuback,
    MQTTSubscribe,
    MQTTUnsuback,
    MQTTUnsubscribe,
    decode_packet,
    decode_variable_byte_integer,
    encode_packet,
    encode_variable_byte_integer,
)


@pytest.mark.parametrize('length', [1, 2, 3])
def test_variable_integer_continuations_remain_incomplete(length):
    with pytest.raises(IncompletePacket):
        decode_variable_byte_integer(b'\x80' * length)


def test_fourth_variable_integer_continuation_is_malformed_without_a_fifth_byte():
    with pytest.raises(MQTTDecodeError):
        decode_variable_byte_integer(b'\x80' * 4)
    assert decode_variable_byte_integer(b'\xff\xff\xff\x7f') == (268435455, 4)


@pytest.mark.parametrize('cap', [0, 1048576])
def test_framer_rejects_fourth_remaining_length_continuation_without_more_input(cap):
    framer = PacketFramer(max_packet_size=cap)
    framer.feed(b'\x30\x80\x80\x80\x80')
    with pytest.raises(MQTTDecodeError):
        list(framer)

# §2.1.3 — PUBREL, SUBSCRIBE and UNSUBSCRIBE reserve these fixed-header flags.


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
    ('pubrel-empty', packet(MQTTPacketType.PUBREL, flags=RESERVED_FLAGS_0010)),
    ('pubcomp-one-octet', packet(MQTTPacketType.PUBCOMP, b'\x01')),
    ('publish-qos1-no-packet-id', packet(
        MQTTPacketType.PUBLISH, _utf8(b'a'), flags=0x2)),
    ('publish-qos1-one-octet-id', packet(
        MQTTPacketType.PUBLISH, _utf8(b'a') + b'\x01', flags=0x2)),
    ('subscribe-no-filter', packet(
        MQTTPacketType.SUBSCRIBE, _ack_body(props=b''), flags=RESERVED_FLAGS_0010)),
    ('unsubscribe-no-filter', packet(
        MQTTPacketType.UNSUBSCRIBE, _ack_body(props=b''), flags=RESERVED_FLAGS_0010)),
    ('suback-no-reason-code', packet(MQTTPacketType.SUBACK, _ack_body(props=b''))),
    ('unsuback-no-reason-code', packet(MQTTPacketType.UNSUBACK, _ack_body(props=b''))),
    ('connack-flags-only', packet(MQTTPacketType.CONNACK, b'\x00')),
    ('connack-two-octets', packet(MQTTPacketType.CONNACK, b'\x01\x00')),
    ('connect-client-id-truncated', packet(MQTTPacketType.CONNECT, _CONNECT_BODY[:-1])),
    ('connect-password-length-cut', packet(MQTTPacketType.CONNECT, (
        b'\x00\x04MQTT\x05\xc0\x00\x3c' + _props() + _utf8(b'x') + _utf8(b'u') + b'\x00'))),
    ('publish-topic-length-cut', packet(MQTTPacketType.PUBLISH, b'\x00')),
    ('connack-property-length-cut', packet(MQTTPacketType.CONNACK, b'\x00\x00\x80')),
]

ZERO_PACKET_IDENTIFIER = [
    ('puback', packet(MQTTPacketType.PUBACK, _ack_body(packet_id=0))),
    ('pubrec', packet(MQTTPacketType.PUBREC, _ack_body(packet_id=0))),
    ('pubrel', packet(MQTTPacketType.PUBREL, _ack_body(packet_id=0),
                      flags=RESERVED_FLAGS_0010)),
    ('pubcomp', packet(MQTTPacketType.PUBCOMP, _ack_body(packet_id=0))),
    ('publish-qos1', packet(MQTTPacketType.PUBLISH,
                            _publish_body(topic=b'a', packet_id=0), flags=0x2)),
    ('subscribe', packet(MQTTPacketType.SUBSCRIBE,
                         _ack_body(packet_id=0, props=b'') + _ONE_FILTER,
                         flags=RESERVED_FLAGS_0010)),
    ('suback', packet(MQTTPacketType.SUBACK,
                      _ack_body(packet_id=0, props=b'') + b'\x00')),
    ('unsubscribe', packet(MQTTPacketType.UNSUBSCRIBE,
                           _ack_body(packet_id=0, props=b'') + _ONE_FILTER,
                           flags=RESERVED_FLAGS_0010)),
    ('unsuback', packet(MQTTPacketType.UNSUBACK,
                        _ack_body(packet_id=0, props=b'') + b'\x00')),
]

BYTES_AFTER_THE_LAST_FIELD = [
    ('puback-after-properties', packet(
        MQTTPacketType.PUBACK, _ack_body(reason=ReasonCode.SUCCESS, props=b'', surplus=b'\xff'))),
    ('pubrel-after-reason', packet(
        MQTTPacketType.PUBREL, _ack_body(reason=ReasonCode.SUCCESS, props=b'', surplus=b'\x00'),
        flags=RESERVED_FLAGS_0010)),
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


@pytest.mark.parametrize('label,wire', SHORTER_THAN_THE_TYPE_REQUIRES + BYTES_AFTER_THE_LAST_FIELD,
                         ids=[case[0] for case in SHORTER_THAN_THE_TYPE_REQUIRES + BYTES_AFTER_THE_LAST_FIELD])
def test_a_body_shorter_than_the_type_requires_is_malformed(label, wire):
    with pytest.raises(MQTTDecodeError):
        decode_packet(wire)


@pytest.mark.parametrize('label,wire', ZERO_PACKET_IDENTIFIER,
                         ids=[case[0] for case in ZERO_PACKET_IDENTIFIER])
def test_a_zero_packet_identifier_is_malformed(label, wire):
    """§2.2.1 — a Packet Identifier of 0 is not allowed."""
    with pytest.raises(MQTTDecodeError):
        decode_packet(wire)


@pytest.mark.parametrize('label,wire', BODY_MUST_BE_ABSENT,
                         ids=[case[0] for case in BODY_MUST_BE_ABSENT])
def test_ping_carries_no_body(label, wire):
    """§3.12, §3.13 — PINGREQ and PINGRESP have no payload."""
    with pytest.raises(MQTTDecodeError):
        decode_packet(wire)


def test_a_malformed_packet_never_reaches_the_framer():
    """The entry point the connection actor reads drops it (§4.13)."""
    for _, wire in (SHORTER_THAN_THE_TYPE_REQUIRES + ZERO_PACKET_IDENTIFIER
                    + BYTES_AFTER_THE_LAST_FIELD + BODY_MUST_BE_ABSENT):
        framer = PacketFramer()
        framer.feed(wire)
        with pytest.raises(MQTTDecodeError):
            list(framer)


def test_the_packet_behind_a_malformed_one_is_not_decoded():
    """Malformed input terminates framing at the original boundary."""
    valid = packet(MQTTPacketType.PUBACK, b'\x00\x01')
    for _, wire in (SHORTER_THAN_THE_TYPE_REQUIRES + ZERO_PACKET_IDENTIFIER
                    + BYTES_AFTER_THE_LAST_FIELD + BODY_MUST_BE_ABSENT):
        framer = PacketFramer()
        framer.feed(wire + valid)
        with pytest.raises(MQTTDecodeError):
            next(iter(framer))


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


def test_non_minimal_variable_byte_integer_encodings_are_accepted():
    """§1.5.5 requires the minimum number of bytes; this runtime does not
    enforce it (BLA-352 residual).  A value whose encoding carries a
    redundant continuation octet decodes successfully — this pins what
    the code does today, so the tolerance cannot change silently."""
    assert decode_variable_byte_integer(b'\x80\x00') == (0, 2)     # 0 in 2 octets
    assert decode_variable_byte_integer(b'\xac\x82\x00') == (300, 3)  # 300 in 3

    # Remaining Length 0 in 2 octets.
    message, consumed = decode_packet(
        bytes([MQTTPacketType.PINGREQ << 4]) + b'\x80\x00')
    assert message.packet_type == MQTTPacketType.PINGREQ
    assert consumed == 3

    # Property Length 0 in 2 octets.
    message, consumed = decode_packet(
        packet(MQTTPacketType.PUBACK, b'\x00\x01' + b'\x00' + b'\x80\x00'))
    assert message == MQTTPuback(packet_id=1, reason_code=ReasonCode.SUCCESS,
                                 properties={})
    assert consumed == 7

    # Subscription Identifier 1 in 2 octets.
    message, consumed = decode_packet(
        packet(MQTTPacketType.PUBLISH, _publish_body(props=b'\x0b\x81\x00')))
    assert message.properties == {'subscription_identifier': 1}
    assert consumed == 9


def _subscription(qos: int, *, no_local: bool = False) -> dict:
    return {'qos': qos, 'no_local': no_local, 'retain_as_published': False,
            'retain_handling': 0}


# Each packet type's canonical wire form, written out from §3 by hand.
WIRE = [
    ('connect', MQTTConnect(client_id='x', clean_start=True, keep_alive=60),
     '100e00044d5154540502003c00000178'),
    ('connect-will-username-password', MQTTConnect(
        client_id='c', clean_start=True, keep_alive=10, username='u', password=b'p',
        will_topic='w', will_payload=b'!', will_qos=1, will_retain=True),
     '101b00044d51545405ee000a0000016300000177000121000175000170'),
    ('connect-keep-alive-max-with-properties', MQTTConnect(
        client_id='x', clean_start=True, keep_alive=65535,
        properties={'session_expiry_interval': 30}),
     '101300044d5154540502ffff05110000001e000178'),
    ('connect-level-4-will', MQTTConnect(
        client_id='x', clean_start=True, keep_alive=60, proto_level=4, will_topic='w',
        will_payload=b'p'), '101300044d5154540406003c000178000177000170'),
    ('connect-level-4-username-only', MQTTConnect(
        client_id='', clean_start=False, keep_alive=0, proto_level=4, username='u'),
     '100f00044d515454048000000000000175'),
    ('connack', MQTTConnack(session_present=True), '2003010000'),
    ('publish-qos0', MQTTPublish(topic='a/b', payload=b'x'), '30070003612f620078'),
    ('publish-qos1-dup-retain', MQTTPublish(topic='a', payload=b'', qos=1, packet_id=10,
                                            dup=True, retain=True), '3b06000161000a00'),
    ('publish-property', MQTTPublish(topic='t', payload=b'p',
                                     properties={'message_expiry_interval': 60}),
     '300a00017405020000003c70'),
    ('puback', MQTTPuback(packet_id=7), '400400070000'),
    ('puback-reason-user-property', MQTTPuback(
        packet_id=1, reason_code=ReasonCode.NO_MATCHING_SUBSCRIBERS,
        properties={'user_properties': [('k', 'v')]}), '400b000110072600016b000176'),
    ('pubrec', MQTTPubrec(packet_id=7), '500400070000'),
    ('pubrel', MQTTPubrel(packet_id=7), '620400070000'),
    ('pubcomp', MQTTPubcomp(packet_id=7), '700400070000'),
    ('subscribe', MQTTSubscribe(packet_id=1, subscriptions=[('a/b', 1)],
                                subscription_options=[_subscription(1, no_local=True)]),
     '82090001000003612f6205'),
    ('suback', MQTTSuback(packet_id=1, reason_codes=[ReasonCode.GRANTED_QOS_1,
                                                     ReasonCode.UNSPECIFIED_ERROR]),
     '90050001000180'),
    ('unsubscribe', MQTTUnsubscribe(packet_id=2, topics=['a']), 'a206000200000161'),
    ('unsuback', MQTTUnsuback(packet_id=2, reason_codes=[ReasonCode.SUCCESS]), 'b00400020000'),
    ('pingreq', MQTTPingreq(), 'c000'),
    ('pingresp', MQTTPingresp(), 'd000'),
    ('disconnect', MQTTDisconnect(), 'e000'),
    ('disconnect-with-will', MQTTDisconnect(reason_code=ReasonCode.DISCONNECT_WITH_WILL),
     'e0020400'),
    ('auth', MQTTAuth(), 'f000'),
    ('publish-256-octet-topic-and-binary', MQTTPublish(
        topic='t' * 256, payload=b'', properties={'correlation_data': b'c' * 256}),
     '308704' + '0100' + '74' * 256 + '8302' + '09' + '0100' + '63' * 256),
    ('auth-continue', MQTTAuth(reason_code=ReasonCode.CONTINUE_AUTHENTICATION, properties={
        'authentication_method': 'm', 'authentication_data': b'd'}), 'f00a18081500016d16000164'),
]


# Shorter legal forms the encoder never writes (§3.4.2.1, §3.14.2.1, §3.15.2.1).
DECODES_AS = [
    ('puback-identifier-only', '40020007', MQTTPuback(packet_id=7)),
    ('puback-reason-only', '4003000710',
     MQTTPuback(packet_id=7, reason_code=ReasonCode.NO_MATCHING_SUBSCRIBERS)),
    ('disconnect-reason-only', 'e00100', MQTTDisconnect(reason_code=ReasonCode.SUCCESS)),
    ('auth-reason-only', 'f00118', MQTTAuth(reason_code=ReasonCode.CONTINUE_AUTHENTICATION)),
]


@pytest.mark.parametrize('label,message,wire', WIRE, ids=[row[0] for row in WIRE])
def test_each_packet_type_has_one_wire_form(label, message, wire):
    wire = bytes.fromhex(wire)
    assert encode_packet(message) == wire
    assert decode_packet(wire) == message
    assert decode_packet(wire)[1] == len(wire)


def test_a_subscription_option_left_out_is_written_as_zero():
    assert encode_packet(MQTTSubscribe(
        packet_id=1, subscriptions=[('a/b', 1)], subscription_options=[{'no_local': True}],
    )).hex() == '82090001000003612f6205'


@pytest.mark.parametrize('value', [-1, 268_435_456])
def test_a_variable_byte_integer_out_of_range_is_refused(value):
    """§1.5.5 — four octets carry at most 268,435,455."""
    with pytest.raises(MQTTDecodeError):
        encode_variable_byte_integer(value)
    assert encode_variable_byte_integer(268_435_455) == b'\xff\xff\xff\x7f'


@pytest.mark.parametrize('label,wire,message', DECODES_AS, ids=[row[0] for row in DECODES_AS])
def test_a_shorter_legal_form_still_decodes(label, wire, message):
    assert decode_packet(bytes.fromhex(wire)) == message
