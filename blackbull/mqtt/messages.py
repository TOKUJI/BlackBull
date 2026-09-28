"""MQTT 5.0 control-packet codec.

Level-A (pure-data) layer for the ``blackbull-mqtt`` broker sidecar: the 15
MQTT 5.0 control packets as frozen dataclasses, a wire encoder/decoder, the
MQTT 5.0 property system, reason codes, and the topic-filter matching
algorithm.  No I/O and no broker state live here — that is the job of
[`blackbull.mqtt.broker`][blackbull.mqtt.broker] and [`blackbull.mqtt.connection`][blackbull.mqtt.connection].

Reference: MQTT Version 5.0, OASIS Standard
  https://docs.oasis-open.org/mqtt/mqtt/v5.0/os/mqtt-v5.0-os.html

Decoder return contract
-----------------------
[`decode_packet`][] returns the decoded message object.  Every message also
unpacks into ``(message, bytes_consumed)`` so a caller walking a buffer of
concatenated packets can advance its offset::

    msg = decode_packet(buf)            # attribute access / isinstance
    msg, consumed = decode_packet(buf)  # buffer-walking

This dual ergonomics is provided by ``MQTTMessage.__iter__``; the consumed
count is recorded on the instance during decode.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum, IntFlag
from typing import Any, Callable, ClassVar, NamedTuple


# ===========================================================================
# Exceptions
# ===========================================================================

class MQTTDecodeError(ValueError):
    """A buffer could not be decoded as a valid MQTT control packet."""


class IncompletePacket(Exception):
    """The buffer does not yet hold a complete packet — read more bytes."""


# ===========================================================================
# §2.1.1 Table 2-1 — Control packet types
# ===========================================================================

class MQTTPacketType(IntEnum):
    """The 15 MQTT 5.0 control packet types (§2.1.1 Table 2-1)."""
    CONNECT = 1
    CONNACK = 2
    PUBLISH = 3
    PUBACK = 4
    PUBREC = 5
    PUBREL = 6
    PUBCOMP = 7
    SUBSCRIBE = 8
    SUBACK = 9
    UNSUBSCRIBE = 10
    UNSUBACK = 11
    PINGREQ = 12
    PINGRESP = 13
    DISCONNECT = 14
    AUTH = 15


def extract_packet_type(first_byte: int) -> int:
    """§2.1.1 — Packet type is bits 7-4 of the first fixed-header byte.

    Type 0 is Reserved/forbidden (§2.1.1 Table 2-1) and raises ``ValueError``.
    """
    ptype = (first_byte >> 4) & 0x0F
    if ptype == 0:
        raise ValueError('Control Packet Type 0 is Reserved/forbidden (§2.1.1)')
    return ptype


def extract_flags(first_byte: int) -> int:
    """§2.1.1 — Flags occupy bits 3-0 of the first fixed-header byte."""
    return first_byte & 0x0F


class PublishFlags(NamedTuple):
    """Decoded PUBLISH fixed-header flags (§3.3.1)."""
    qos: int
    dup: bool
    retain: bool


# ---------------------------------------------------------------------------
# Protocol level & flag-byte bit definitions — so the bitwise codec below
# reads in MQTT 5.0 spec terms rather than raw hex (§3.1.2.2, §3.1.2.3,
# §3.3.1, §3.8.3.1).  A two-bit QoS / Retain-Handling subfield is expressed
# as a (shift, mask) pair; single bits are [`IntFlag`][enum.IntFlag] members.
# ---------------------------------------------------------------------------

class ProtocolLevel(IntEnum):
    """CONNECT Protocol Level (§3.1.2.2).  This broker speaks ``V5_0``."""
    V3_1 = 3
    V3_1_1 = 4
    V5_0 = 5


class ConnectFlags(IntFlag):
    """Single-bit flags in the CONNECT flags byte (§3.1.2.3).

    The Will QoS field is the two-bit subfield at ``WILL_QOS_SHIFT``
    (mask ``WILL_QOS_MASK``), not a flag here.
    """
    CLEAN_START = 0x02
    WILL_FLAG = 0x04
    WILL_RETAIN = 0x20
    PASSWORD = 0x40
    USERNAME = 0x80


WILL_QOS_SHIFT = 3          # §3.1.2.6 — Will QoS occupies bits 4-3
WILL_QOS_MASK = 0x03


class PublishFlagBits(IntFlag):
    """Single-bit flags in the PUBLISH fixed header (§3.3.1).

    QoS is the two-bit subfield at ``PUBLISH_QOS_SHIFT``.
    """
    RETAIN = 0x01
    DUP = 0x08


PUBLISH_QOS_SHIFT = 1       # §3.3.1.2 — QoS occupies bits 2-1
PUBLISH_QOS_MASK = 0x03


class SubscriptionOptions(IntFlag):
    """Single-bit options in the SUBSCRIBE options byte (§3.8.3.1)."""
    NO_LOCAL = 0x04
    RETAIN_AS_PUBLISHED = 0x08


SUBSCRIPTION_QOS_MASK = 0x03    # §3.8.3.1 — Maximum QoS, bits 1-0
RETAIN_HANDLING_SHIFT = 4       # §3.8.3.1 — Retain Handling, bits 5-4
RETAIN_HANDLING_MASK = 0x03

# §2.1.3 — PUBREL/SUBSCRIBE/UNSUBSCRIBE carry mandatory fixed-header flags
# 0b0010; every other non-PUBLISH packet's flags MUST be 0b0000.
RESERVED_FLAGS_0010 = 0x02

# §3.1.2.11.2 — 0xFFFFFFFF: the Session does not expire.
SESSION_EXPIRY_NEVER = 0xFFFFFFFF


def decode_publish_flags(flags_byte: int) -> PublishFlags:
    """§3.3.1 — DUP (bit 3), QoS (bits 2-1), RETAIN (bit 0).

    QoS is two bits, so the field can hold 3, and §3.3.1-4 says a PUBLISH
    with both QoS bits set is a **Malformed Packet**.  Masking alone
    returned it as a value: nothing downstream acknowledged qos 3 (only 1
    and 2 have an ack path), so such a packet was routed and retained with
    no acknowledgement at all -- delivered, and invisible.
    """
    qos = (flags_byte >> PUBLISH_QOS_SHIFT) & PUBLISH_QOS_MASK
    if qos > 2:
        raise MQTTDecodeError(
            f'PUBLISH with QoS {qos}: both QoS bits set is a Malformed '
            f'Packet (MQTT 5 §3.3.1-4)')
    return PublishFlags(
        qos=qos,
        dup=bool(flags_byte & PublishFlagBits.DUP),
        retain=bool(flags_byte & PublishFlagBits.RETAIN),
    )


# ===========================================================================
# §4 — Reason codes
# ===========================================================================

class ReasonCode(IntEnum):
    """§2.4 reason codes — the one definition of each value and name."""
    SUCCESS = 0x00
    GRANTED_QOS_1 = 0x01
    GRANTED_QOS_2 = 0x02
    DISCONNECT_WITH_WILL = 0x04
    NO_MATCHING_SUBSCRIBERS = 0x10
    NO_SUBSCRIPTION_EXISTED = 0x11
    CONTINUE_AUTHENTICATION = 0x18
    REAUTHENTICATE = 0x19
    UNSPECIFIED_ERROR = 0x80
    MALFORMED_PACKET = 0x81
    PROTOCOL_ERROR = 0x82
    IMPLEMENTATION_SPECIFIC_ERROR = 0x83
    UNSUPPORTED_PROTOCOL_VERSION = 0x84
    CLIENT_IDENTIFIER_NOT_VALID = 0x85
    BAD_USER_NAME_OR_PASSWORD = 0x86
    NOT_AUTHORIZED = 0x87
    SERVER_UNAVAILABLE = 0x88
    SERVER_BUSY = 0x89
    BANNED = 0x8A
    SERVER_SHUTTING_DOWN = 0x8B
    BAD_AUTHENTICATION_METHOD = 0x8C
    KEEP_ALIVE_TIMEOUT = 0x8D
    SESSION_TAKEN_OVER = 0x8E
    TOPIC_FILTER_INVALID = 0x8F
    TOPIC_NAME_INVALID = 0x90
    PACKET_IDENTIFIER_IN_USE = 0x91
    PACKET_IDENTIFIER_NOT_FOUND = 0x92
    RECEIVE_MAXIMUM_EXCEEDED = 0x93
    TOPIC_ALIAS_INVALID = 0x94
    PACKET_TOO_LARGE = 0x95
    MESSAGE_RATE_TOO_HIGH = 0x96
    QUOTA_EXCEEDED = 0x97
    ADMINISTRATIVE_ACTION = 0x98
    PAYLOAD_FORMAT_INVALID = 0x99
    RETAIN_NOT_SUPPORTED = 0x9A
    QOS_NOT_SUPPORTED = 0x9B
    USE_ANOTHER_SERVER = 0x9C
    SERVER_MOVED = 0x9D
    SHARED_SUBSCRIPTIONS_NOT_SUPPORTED = 0x9E
    CONNECTION_RATE_EXCEEDED = 0x9F
    MAXIMUM_CONNECT_TIME = 0xA0
    SUBSCRIPTION_IDENTIFIERS_NOT_SUPPORTED = 0xA1
    WILDCARD_SUBSCRIPTIONS_NOT_SUPPORTED = 0xA2


# §2.4 display names — keyed by member, so each value lives only in
# `ReasonCode` and cannot drift from its name.
_REASON_CODE_LABELS: dict[ReasonCode, str] = {
    ReasonCode.SUCCESS: 'Success',
    ReasonCode.GRANTED_QOS_1: 'Granted QoS 1',
    ReasonCode.GRANTED_QOS_2: 'Granted QoS 2',
    ReasonCode.DISCONNECT_WITH_WILL: 'Disconnect with Will Message',
    ReasonCode.NO_MATCHING_SUBSCRIBERS: 'No matching subscribers',
    ReasonCode.NO_SUBSCRIPTION_EXISTED: 'No subscription existed',
    ReasonCode.CONTINUE_AUTHENTICATION: 'Continue authentication',
    ReasonCode.REAUTHENTICATE: 'Re-authenticate',
    ReasonCode.UNSPECIFIED_ERROR: 'Unspecified error',
    ReasonCode.MALFORMED_PACKET: 'Malformed Packet',
    ReasonCode.PROTOCOL_ERROR: 'Protocol Error',
    ReasonCode.IMPLEMENTATION_SPECIFIC_ERROR: 'Implementation specific error',
    ReasonCode.UNSUPPORTED_PROTOCOL_VERSION: 'Unsupported Protocol Version',
    ReasonCode.CLIENT_IDENTIFIER_NOT_VALID: 'Client Identifier not valid',
    ReasonCode.BAD_USER_NAME_OR_PASSWORD: 'Bad User Name or Password',
    ReasonCode.NOT_AUTHORIZED: 'Not authorized',
    ReasonCode.SERVER_UNAVAILABLE: 'Server unavailable',
    ReasonCode.SERVER_BUSY: 'Server busy',
    ReasonCode.BANNED: 'Banned',
    ReasonCode.SERVER_SHUTTING_DOWN: 'Server shutting down',
    ReasonCode.BAD_AUTHENTICATION_METHOD: 'Bad authentication method',
    ReasonCode.KEEP_ALIVE_TIMEOUT: 'Keep Alive timeout',
    ReasonCode.SESSION_TAKEN_OVER: 'Session taken over',
    ReasonCode.TOPIC_FILTER_INVALID: 'Topic Filter invalid',
    ReasonCode.TOPIC_NAME_INVALID: 'Topic Name invalid',
    ReasonCode.PACKET_IDENTIFIER_IN_USE: 'Packet Identifier in use',
    ReasonCode.PACKET_IDENTIFIER_NOT_FOUND: 'Packet Identifier not found',
    ReasonCode.RECEIVE_MAXIMUM_EXCEEDED: 'Receive Maximum exceeded',
    ReasonCode.TOPIC_ALIAS_INVALID: 'Topic Alias invalid',
    ReasonCode.PACKET_TOO_LARGE: 'Packet too large',
    ReasonCode.MESSAGE_RATE_TOO_HIGH: 'Message rate too high',
    ReasonCode.QUOTA_EXCEEDED: 'Quota exceeded',
    ReasonCode.ADMINISTRATIVE_ACTION: 'Administrative action',
    ReasonCode.PAYLOAD_FORMAT_INVALID: 'Payload format invalid',
    ReasonCode.RETAIN_NOT_SUPPORTED: 'Retain not supported',
    ReasonCode.QOS_NOT_SUPPORTED: 'QoS not supported',
    ReasonCode.USE_ANOTHER_SERVER: 'Use another server',
    ReasonCode.SERVER_MOVED: 'Server moved',
    ReasonCode.SHARED_SUBSCRIPTIONS_NOT_SUPPORTED: 'Shared Subscriptions not supported',
    ReasonCode.CONNECTION_RATE_EXCEEDED: 'Connection rate exceeded',
    ReasonCode.MAXIMUM_CONNECT_TIME: 'Maximum connect time',
    ReasonCode.SUBSCRIPTION_IDENTIFIERS_NOT_SUPPORTED: 'Subscription Identifiers not supported',
    ReasonCode.WILDCARD_SUBSCRIPTIONS_NOT_SUPPORTED: 'Wildcard Subscriptions not supported',
}

assert set(_REASON_CODE_LABELS) == set(ReasonCode), \
    'every §2.4 code carries exactly one label'

# §2.4 — この値以上がエラー側。閾値は UNSPECIFIED_ERROR から導く。
_ERROR_THRESHOLD = int(ReasonCode.UNSPECIFIED_ERROR)


class MQTTReasonCode(int):
    """An MQTT 5.0 reason code (§2.4) as received.

    A thin ``int`` subclass so any byte value (0-255) is representable without
    raising — undefined codes report ``name == 'Unknown'``.  Known values name
    themselves through [`ReasonCode`][].
    """

    def __new__(cls, value: int) -> 'MQTTReasonCode':
        return super().__new__(cls, value)

    @property
    def value(self) -> int:
        return int(self)

    @property
    def name(self) -> str:  # type: ignore[override]
        return _REASON_CODE_LABELS.get(self, 'Unknown')

    @property
    def is_success(self) -> bool:
        return self < _ERROR_THRESHOLD

    @property
    def is_error(self) -> bool:
        return self >= _ERROR_THRESHOLD

    def __repr__(self) -> str:
        return f'MQTTReasonCode(0x{int(self):02X}: {self.name})'


# ===========================================================================
# §1.5.5 / §2.2.1 — Variable Byte Integer
# ===========================================================================

def encode_variable_byte_integer(value: int) -> bytes:
    """§1.5.5 — Encode an int (0..268,435,455) as a Variable Byte Integer."""
    if value < 0 or value > 268_435_455:
        raise MQTTDecodeError(f'Variable Byte Integer out of range: {value}')
    out = bytearray()
    while True:
        byte = value & 0x7F          # low 7 bits  (== value % 128)
        value >>= 7                  # next group  (== value // 128)
        if value > 0:
            byte |= 0x80             # continuation bit
        out.append(byte)
        if value == 0:
            break
    return bytes(out)


def _read_vbi_at(data: bytes | bytearray, pos: int, end: int) -> tuple[int, int]:
    """§1.5.5 — Decode a Variable Byte Integer in place; no buffer copies.

    Returns ``(value, new_pos)``.  An integer still continuing when *end*
    arrives is an incomplete read; one continuing on its fourth octet is
    malformed.  Trailing bytes beyond the integer are the caller's.
    """
    multiplier = 1
    value = 0
    consumed = 0
    while pos < end:
        byte = data[pos]
        pos += 1
        value += (byte & 0x7F) * multiplier
        consumed += 1
        if (byte & 0x80) == 0:
            return value, pos
        if consumed == 4:
            raise MQTTDecodeError('Variable Byte Integer too long')
        multiplier *= 128
    raise IncompletePacket('Variable Byte Integer continues past buffer')


def decode_variable_byte_integer(data: bytes) -> tuple[int, int]:
    """§1.5.5 — Decode a Variable Byte Integer; return ``(value, consumed)``.

    Trailing bytes beyond the integer are ignored (the caller tracks them).
    """
    value, pos = _read_vbi_at(data, 0, len(data))
    return value, pos


# ===========================================================================
# §1.5 — Primitive field codecs
# ===========================================================================

def _encode_utf8(text: str) -> bytes:
    raw = text.encode('utf-8')
    return len(raw).to_bytes(2, 'big') + raw


def _decode_utf8(data: bytes, offset: int) -> tuple[str, int]:
    if offset + 2 > len(data):
        raise IncompletePacket('UTF-8 length prefix truncated')
    length = int.from_bytes(data[offset:offset + 2], 'big')
    start = offset + 2
    end = start + length
    if end > len(data):
        raise IncompletePacket('UTF-8 body truncated')
    return data[start:end].decode('utf-8'), end


def _encode_binary(blob: bytes) -> bytes:
    return len(blob).to_bytes(2, 'big') + blob


def _decode_binary(data: bytes, offset: int) -> tuple[bytes, int]:
    if offset + 2 > len(data):
        raise IncompletePacket('Binary length prefix truncated')
    length = int.from_bytes(data[offset:offset + 2], 'big')
    start = offset + 2
    end = start + length
    if end > len(data):
        raise IncompletePacket('Binary body truncated')
    return bytes(data[start:end]), end


# ===========================================================================
# §2.2.2 — Properties
# ===========================================================================

# Wire-type categories for properties.
_BYTE, _UINT16, _UINT32, _VBI, _UTF8, _BINARY, _PAIR = (
    'byte', 'uint16', 'uint32', 'vbi', 'utf8', 'binary', 'pair'
)


class PropertyId(IntEnum):
    """§2.2.2.2 Table 2-3 property identifiers."""
    PAYLOAD_FORMAT_INDICATOR = 0x01
    MESSAGE_EXPIRY_INTERVAL = 0x02
    CONTENT_TYPE = 0x03
    RESPONSE_TOPIC = 0x08
    CORRELATION_DATA = 0x09
    SUBSCRIPTION_IDENTIFIER = 0x0B
    SESSION_EXPIRY_INTERVAL = 0x11
    ASSIGNED_CLIENT_IDENTIFIER = 0x12
    SERVER_KEEP_ALIVE = 0x13
    AUTHENTICATION_METHOD = 0x15
    AUTHENTICATION_DATA = 0x16
    REQUEST_PROBLEM_INFORMATION = 0x17
    WILL_DELAY_INTERVAL = 0x18
    REQUEST_RESPONSE_INFORMATION = 0x19
    RESPONSE_INFORMATION = 0x1A
    SERVER_REFERENCE = 0x1C
    REASON_STRING = 0x1F
    RECEIVE_MAXIMUM = 0x21
    TOPIC_ALIAS_MAXIMUM = 0x22
    TOPIC_ALIAS = 0x23
    MAXIMUM_QOS = 0x24
    RETAIN_AVAILABLE = 0x25
    USER_PROPERTY = 0x26
    MAXIMUM_PACKET_SIZE = 0x27
    WILDCARD_SUBSCRIPTION_AVAILABLE = 0x28
    SUBSCRIPTION_IDENTIFIER_AVAILABLE = 0x29
    SHARED_SUBSCRIPTION_AVAILABLE = 0x2A


class PropertyInfo(NamedTuple):
    """Static description of one MQTT 5.0 property identifier (§2.2.2.2)."""
    identifier: PropertyId
    name: str
    wire_type: str


# (id, identifier-name, runtime dict key, wire type).  The identifier name is
# the §2.2.2.2 Table 2-3 name; the runtime key is what appears in a message's
# ``properties`` dict (identical except User Property, which aggregates into
# the plural ``user_properties`` list of (k, v) pairs).
_PROPERTY_SPECS: tuple[tuple[int, str, str, str], ...] = (
    (PropertyId.PAYLOAD_FORMAT_INDICATOR,
     'payload_format_indicator', 'payload_format_indicator', _BYTE),
    (PropertyId.MESSAGE_EXPIRY_INTERVAL,
     'message_expiry_interval', 'message_expiry_interval', _UINT32),
    (PropertyId.CONTENT_TYPE, 'content_type', 'content_type', _UTF8),
    (PropertyId.RESPONSE_TOPIC, 'response_topic', 'response_topic', _UTF8),
    (PropertyId.CORRELATION_DATA,
     'correlation_data', 'correlation_data', _BINARY),
    (PropertyId.SUBSCRIPTION_IDENTIFIER,
     'subscription_identifier', 'subscription_identifier', _VBI),
    (PropertyId.SESSION_EXPIRY_INTERVAL,
     'session_expiry_interval', 'session_expiry_interval', _UINT32),
    (PropertyId.ASSIGNED_CLIENT_IDENTIFIER,
     'assigned_client_identifier', 'assigned_client_identifier', _UTF8),
    (PropertyId.SERVER_KEEP_ALIVE,
     'server_keep_alive', 'server_keep_alive', _UINT16),
    (PropertyId.AUTHENTICATION_METHOD,
     'authentication_method', 'authentication_method', _UTF8),
    (PropertyId.AUTHENTICATION_DATA,
     'authentication_data', 'authentication_data', _BINARY),
    (PropertyId.REQUEST_PROBLEM_INFORMATION,
     'request_problem_information', 'request_problem_information', _BYTE),
    (PropertyId.WILL_DELAY_INTERVAL,
     'will_delay_interval', 'will_delay_interval', _UINT32),
    (PropertyId.REQUEST_RESPONSE_INFORMATION,
     'request_response_information', 'request_response_information', _BYTE),
    (PropertyId.RESPONSE_INFORMATION,
     'response_information', 'response_information', _UTF8),
    (PropertyId.SERVER_REFERENCE,
     'server_reference', 'server_reference', _UTF8),
    (PropertyId.REASON_STRING, 'reason_string', 'reason_string', _UTF8),
    (PropertyId.RECEIVE_MAXIMUM,
     'receive_maximum', 'receive_maximum', _UINT16),
    (PropertyId.TOPIC_ALIAS_MAXIMUM,
     'topic_alias_maximum', 'topic_alias_maximum', _UINT16),
    (PropertyId.TOPIC_ALIAS, 'topic_alias', 'topic_alias', _UINT16),
    (PropertyId.MAXIMUM_QOS, 'maximum_qos', 'maximum_qos', _BYTE),
    (PropertyId.RETAIN_AVAILABLE,
     'retain_available', 'retain_available', _BYTE),
    (PropertyId.USER_PROPERTY, 'user_property', 'user_properties', _PAIR),
    (PropertyId.MAXIMUM_PACKET_SIZE,
     'maximum_packet_size', 'maximum_packet_size', _UINT32),
    (PropertyId.WILDCARD_SUBSCRIPTION_AVAILABLE,
     'wildcard_subscription_available', 'wildcard_subscription_available', _BYTE),
    (PropertyId.SUBSCRIPTION_IDENTIFIER_AVAILABLE,
     'subscription_identifier_available', 'subscription_identifier_available', _BYTE),
    (PropertyId.SHARED_SUBSCRIPTION_AVAILABLE,
     'shared_subscription_available', 'shared_subscription_available', _BYTE),
)

# §2.2.2.2 Table 2-3 — {identifier: name}.  Exactly 27 entries.
PROPERTY_IDENTIFIERS: dict[int, str] = {
    int(pid): ident for pid, ident, _key, _wt in _PROPERTY_SPECS
}

# wire の int で引く表なのでキーは int に正規化する(値は enum から導出)。
_PROP_BY_ID: dict[int, PropertyInfo] = {
    int(pid): PropertyInfo(pid, ident, wt)
    for pid, ident, _key, wt in _PROPERTY_SPECS
}
_PROP_BY_KEY: dict[str, tuple[int, str]] = {
    key: (pid, wt) for pid, _ident, key, wt in _PROPERTY_SPECS
}
_PROP_ID_TO_KEY: dict[int, str] = {
    int(pid): key for pid, _ident, key, _wt in _PROPERTY_SPECS
}


def get_property_info(identifier: int) -> PropertyInfo | None:
    """Return the [`PropertyInfo`][] for a property identifier, or None."""
    return _PROP_BY_ID.get(identifier)


def _decode_vbi_at(data: bytes, pos: int, end: int) -> tuple[int, int]:
    try:
        return _read_vbi_at(data, pos, end)
    except IncompletePacket as exc:
        raise MQTTDecodeError(_CROSSING) from exc


# §2.2.2 contract for the decoders below: consume only bytes inside the
# declared Property Length.  A violation is MQTTDecodeError, never
# IncompletePacket — the packet is already whole; nothing waits for more bytes.
_CROSSING = 'property value crosses the declared Property Length'


def _decode_u8_at(data: bytes, pos: int, end: int) -> tuple[int, int]:
    if pos + 1 > end:
        raise MQTTDecodeError(_CROSSING)
    return data[pos], pos + 1


def _decode_u16_at(data: bytes, pos: int, end: int) -> tuple[int, int]:
    if pos + 2 > end:
        raise MQTTDecodeError(_CROSSING)
    return data[pos] << 8 | data[pos + 1], pos + 2


def _decode_u32_at(data: bytes, pos: int, end: int) -> tuple[int, int]:
    if pos + 4 > end:
        raise MQTTDecodeError(_CROSSING)
    return (data[pos] << 24 | data[pos + 1] << 16
            | data[pos + 2] << 8 | data[pos + 3]), pos + 4


def _decode_utf8_at(data: bytes, pos: int, end: int) -> tuple[str, int]:
    if pos + 2 > end:
        raise MQTTDecodeError(_CROSSING)
    length = int.from_bytes(data[pos:pos + 2], 'big')
    start = pos + 2
    stop = start + length
    if stop > end:
        raise MQTTDecodeError(_CROSSING)
    return data[start:stop].decode('utf-8'), stop


def _decode_binary_at(data: bytes, pos: int, end: int) -> tuple[bytes, int]:
    if pos + 2 > end:
        raise MQTTDecodeError(_CROSSING)
    length = int.from_bytes(data[pos:pos + 2], 'big')
    start = pos + 2
    stop = start + length
    if stop > end:
        raise MQTTDecodeError(_CROSSING)
    return bytes(data[start:stop]), stop


def _decode_pair_at(data: bytes, pos: int, end: int) -> tuple[tuple[str, str], int]:
    pair_key, pos = _decode_utf8_at(data, pos, end)
    pair_val, pos = _decode_utf8_at(data, pos, end)
    return (pair_key, pair_val), pos


# Wire type → (value encoder, value decoder); ``_PAIR`` is not an entry —
# encode guards it, decode binds ``_decode_pair_at``.  Encoders return the
# value bytes *without* the property-id prefix; decoders follow
# ``(data, pos, end) -> (value, new_pos)``, staying inside the section.  This
# table owns wire shape and bounds only; admissibility, multiplicity (User
# Property is plural, the rest single) and value ranges are the message's
# and the broker's.
_WIRE_CODECS: dict[str, tuple[Callable[[Any], bytes],
                              Callable[[bytes, int, int], tuple[Any, int]]]] = {
    _BYTE:   (lambda v: bytes([v & 0xFF]), _decode_u8_at),
    _UINT16: (lambda v: int(v).to_bytes(2, 'big'), _decode_u16_at),
    _UINT32: (lambda v: int(v).to_bytes(4, 'big'), _decode_u32_at),
    _VBI:    (lambda v: encode_variable_byte_integer(int(v)), _decode_vbi_at),
    _UTF8:   (_encode_utf8, _decode_utf8_at),
    _BINARY: (_encode_binary, _decode_binary_at),
}
# Key and decoder are static per identifier: one lookup serves the decode
# loop where separate key and wire-type hops would do three.
_PROP_DECODE: dict[int, tuple[str, Callable[[bytes, int, int], tuple[Any, int]]]] = {
    int(pid): (_PROP_ID_TO_KEY[pid],
               _decode_pair_at if wt == _PAIR else _WIRE_CODECS[wt][1])
    for pid, _ident, _key, wt in _PROPERTY_SPECS
}


def _encode_prop_value(pid: int, wire_type: str, value: Any) -> bytes:
    codec = _WIRE_CODECS.get(wire_type)
    if codec is None:
        raise MQTTDecodeError(f'Unhandled property wire type {wire_type!r}')
    return bytes([pid]) + codec[0](value)


def encode_properties(properties: dict[str, Any]) -> bytes:
    """§2.2.2 — Encode a properties dict to ``Property Length`` + body."""
    body = bytearray()
    for key, value in properties.items():
        spec = _PROP_BY_KEY.get(key)
        if spec is None:
            raise MQTTDecodeError(f'Unknown property {key!r}')
        pid, wire_type = spec
        if wire_type == _PAIR:
            for pair_key, pair_val in value:
                body += bytes([pid]) + _encode_utf8(pair_key) + _encode_utf8(pair_val)
        else:
            body += _encode_prop_value(pid, wire_type, value)
    return encode_variable_byte_integer(len(body)) + bytes(body)


def decode_properties(data: bytes, offset: int = 0) -> tuple[dict[str, Any], int]:
    """§2.2.2 — Decode a properties block; return ``(props, consumed)``.

    ``consumed`` counts the Property Length prefix plus the property bytes.
    """
    length, start = _read_vbi_at(data, offset, len(data))
    end = start + length
    if end > len(data):
        raise IncompletePacket('Properties body truncated')
    props: dict[str, Any] = {}
    pos = start
    while pos < end:
        pid = data[pos]
        pos += 1
        entry = _PROP_DECODE.get(pid)
        if entry is None:
            raise MQTTDecodeError(f'Unknown property identifier 0x{pid:02X}')
        runtime_key, decoder = entry
        value, pos = decoder(data, pos, end)
        if decoder is _decode_pair_at:
            props.setdefault(runtime_key, []).append(value)
        else:
            props[runtime_key] = value
    return props, end - offset


# ===========================================================================
# Message dataclasses
# ===========================================================================

class MQTTMessage:
    """Base for all MQTT control-packet dataclasses.

    Provides the dual decode contract: a decoded message unpacks into
    ``(message, bytes_consumed)``.  The byte count is set by
    [`decode_packet`][] via ``_set_consumed``; messages built by hand
    report ``0``.
    """

    packet_type: ClassVar[MQTTPacketType]
    _consumed: int = 0

    def _set_consumed(self, n: int) -> None:
        object.__setattr__(self, '_consumed', n)

    def __iter__(self):
        yield self
        yield getattr(self, '_consumed', 0)

    def __getitem__(self, index: int):
        # Mirror the ``(message, bytes_consumed)`` tuple shape so callers may
        # also write ``decode_packet(buf)[0]`` / ``[1]``.
        if index == 0:
            return self
        if index == 1:
            return getattr(self, '_consumed', 0)
        raise IndexError(index)


@dataclass(frozen=True)
class MQTTConnect(MQTTMessage):
    """CONNECT — the client's opening packet (§3.1).

    Carries the session identity (``client_id``, ``clean_start``), the
    keep-alive interval in seconds, optional credentials, and the Will the
    broker publishes if the connection ends without a DISCONNECT.

    Construction rejects two shapes the spec forbids: a ``client_id``
    containing a null character (§1.5.4.2), and a ``password`` without a
    ``username`` (§3.1.2.9).
    """

    packet_type: ClassVar[MQTTPacketType] = MQTTPacketType.CONNECT
    client_id: str
    clean_start: bool
    keep_alive: int
    # int, not the enum: a level this enum does not name must still decode
    # (§3.1.2.2), and ``.value`` keeps repr and asdict at the wire number.
    proto_level: int = ProtocolLevel.V5_0.value
    username: str | None = None
    password: bytes | str | None = None
    will_topic: str | None = None
    will_payload: bytes | None = None
    will_qos: int = 0
    will_retain: bool = False
    will_properties: dict[str, Any] = field(default_factory=dict)
    properties: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        # §1.5.4.2 — a null character (U+0000) MUST NOT appear in a UTF-8 string.
        if '\x00' in self.client_id:
            raise ValueError('Client Identifier must not contain a null character')
        # §3.1.2.9 — the Password Flag MUST NOT be set without the User Name Flag.
        if self.password is not None and self.username is None:
            raise ValueError(
                'Password must not be set without a User Name (§3.1.2.9)')


@dataclass(frozen=True)
class MQTTConnack(MQTTMessage):
    """CONNACK — the broker's answer to CONNECT (§3.2).

    ``session_present`` tells the client whether the broker resumed its
    stored session or started a fresh one; a non-zero ``reason_code`` means
    the connection was refused and the broker closes it.
    """

    packet_type: ClassVar[MQTTPacketType] = MQTTPacketType.CONNACK
    session_present: bool = False
    reason_code: int = ReasonCode.SUCCESS
    properties: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class MQTTPublish(MQTTMessage):
    """PUBLISH — an application message on a topic (§3.3).

    ``qos`` selects the delivery handshake that follows: none for 0, PUBACK
    for 1, PUBREC/PUBREL/PUBCOMP for 2.  ``retain`` asks the broker to keep
    this message as the topic's last known value; ``dup`` marks a redelivery.

    Construction enforces what the payload and properties must agree on: a
    QoS 1 or 2 packet needs a ``packet_id`` (§3.3.2-2), a ``topic_alias`` of 0
    is prohibited (§3.3.2.3.4), and ``payload_format_indicator`` 1 requires
    the payload to decode as UTF-8 (§3.3.2.3.2).
    """

    packet_type: ClassVar[MQTTPacketType] = MQTTPacketType.PUBLISH
    topic: str
    payload: bytes
    qos: int = 0
    packet_id: int | None = None
    retain: bool = False
    dup: bool = False
    properties: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        # §3.3.2-2 / §3.3.2-3 — a QoS 1 or 2 PUBLISH MUST carry a Packet
        # Identifier.
        if self.qos > 0 and self.packet_id is None:
            raise ValueError(
                'QoS > 0 PUBLISH requires a Packet Identifier (§3.3.2-2)')
        # §3.3.2.3.4 — a Topic Alias of 0 is prohibited.
        if self.properties.get('topic_alias') == 0:
            raise ValueError('Topic Alias 0 is prohibited in PUBLISH (§3.3.2.4)')
        # §3.3.2.3.2 — Payload Format Indicator 1 means the payload MUST be
        # valid UTF-8.
        if self.properties.get('payload_format_indicator') == 1:
            try:
                self.payload.decode('utf-8')
            except (UnicodeDecodeError, AttributeError) as exc:
                raise ValueError(
                    'Payload Format Indicator 1 requires a valid UTF-8 payload') from exc


@dataclass(frozen=True)
class _PacketIdAck(MQTTMessage):
    """Shared shape for PUBACK/PUBREC/PUBREL/PUBCOMP (§3.4-§3.7)."""
    packet_id: int
    reason_code: int = ReasonCode.SUCCESS
    properties: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class MQTTPuback(_PacketIdAck):
    """PUBACK — the QoS 1 acknowledgement that completes a delivery (§3.4)."""

    packet_type: ClassVar[MQTTPacketType] = MQTTPacketType.PUBACK


@dataclass(frozen=True)
class MQTTPubrec(_PacketIdAck):
    """PUBREC — first leg of the QoS 2 handshake: the PUBLISH was received
    and a PUBREL is now expected for that packet id (§3.5)."""

    packet_type: ClassVar[MQTTPacketType] = MQTTPacketType.PUBREC


@dataclass(frozen=True)
class MQTTPubrel(_PacketIdAck):
    """PUBREL — second leg of the QoS 2 handshake: release the packet id, to
    be answered with PUBCOMP (§3.6)."""

    packet_type: ClassVar[MQTTPacketType] = MQTTPacketType.PUBREL


@dataclass(frozen=True)
class MQTTPubcomp(_PacketIdAck):
    """PUBCOMP — the QoS 2 handshake is finished and the packet id is free to
    reuse (§3.7)."""

    packet_type: ClassVar[MQTTPacketType] = MQTTPacketType.PUBCOMP


@dataclass(frozen=True)
class MQTTSubscribe(MQTTMessage):
    """SUBSCRIBE — ask for one or more topic filters (§3.8).

    ``subscriptions`` pairs each filter with its maximum QoS.  ``packet_id``
    defaults to ``None`` only so the field can be passed by keyword; §3.8.2
    requires one, and construction without it raises.

    ``subscription_options`` is the §3.8.3.1 per-entry options — ``no_local``,
    ``retain_as_published``, ``retain_handling`` — as one dict per entry in
    ``subscriptions``.  Decoding always fills it; hand-built packets may leave
    it ``None`` to take the defaults.
    """

    packet_type: ClassVar[MQTTPacketType] = MQTTPacketType.SUBSCRIBE
    packet_id: int | None = None
    subscriptions: list[tuple[str, int]] = field(default_factory=list)
    properties: dict[str, Any] = field(default_factory=dict)
    # Per-subscription options (§3.8.3.1): one dict per entry in
    # ``subscriptions`` with keys ``no_local`` / ``retain_as_published`` /
    # ``retain_handling``.  Populated on decode; optional on construction.
    subscription_options: list[dict[str, Any]] | None = None

    def __post_init__(self) -> None:
        if self.packet_id is None:
            raise ValueError('SUBSCRIBE requires a Packet Identifier (§3.8.2)')


@dataclass(frozen=True)
class MQTTSuback(MQTTMessage):
    """SUBACK — one reason code per SUBSCRIBE filter, in the order they were
    requested; a code of 0-2 is the granted QoS and anything higher is a
    refusal of that filter alone (§3.9)."""

    packet_type: ClassVar[MQTTPacketType] = MQTTPacketType.SUBACK
    packet_id: int
    reason_codes: list[int]
    properties: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class MQTTUnsubscribe(MQTTMessage):
    """UNSUBSCRIBE — drop the listed topic filters (§3.10).

    ``topics`` holds the filters as subscribed, matched literally rather than
    by wildcard expansion.  ``packet_id`` defaults to ``None`` only so the
    field can be passed by keyword; §3.10.2 requires one, and construction
    without it raises.
    """

    packet_type: ClassVar[MQTTPacketType] = MQTTPacketType.UNSUBSCRIBE
    packet_id: int | None = None
    topics: list[str] = field(default_factory=list)
    properties: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.packet_id is None:
            raise ValueError('UNSUBSCRIBE requires a Packet Identifier (§3.10.2)')


@dataclass(frozen=True)
class MQTTUnsuback(MQTTMessage):
    """UNSUBACK — one reason code per UNSUBSCRIBE filter, in the order they
    were listed (§3.11)."""

    packet_type: ClassVar[MQTTPacketType] = MQTTPacketType.UNSUBACK
    packet_id: int
    reason_codes: list[int]
    properties: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class MQTTPingreq(MQTTMessage):
    """PINGREQ — the client's keep-alive probe; two octets, no body (§3.12)."""

    packet_type: ClassVar[MQTTPacketType] = MQTTPacketType.PINGREQ


@dataclass(frozen=True)
class MQTTPingresp(MQTTMessage):
    """PINGRESP — the broker's answer to PINGREQ; two octets, no body
    (§3.13)."""

    packet_type: ClassVar[MQTTPacketType] = MQTTPacketType.PINGRESP


@dataclass(frozen=True)
class MQTTDisconnect(MQTTMessage):
    """DISCONNECT — either side announcing the connection is ending (§3.14).

    Sent by a client, it also suppresses the Will unless the reason code says
    otherwise.  ``reason_code`` of ``None`` with no properties encodes as an
    empty body, which the spec reads as Normal disconnection (0).
    """

    packet_type: ClassVar[MQTTPacketType] = MQTTPacketType.DISCONNECT
    reason_code: int | None = None
    properties: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class MQTTAuth(MQTTMessage):
    """AUTH — one exchange of an enhanced-authentication conversation (§3.15).

    The method and any challenge or response data travel in ``properties``;
    the reason code says whether authentication is complete or another round
    is expected.  ``None`` with no properties encodes as an empty body, which
    the spec reads as Success (0).
    """

    packet_type: ClassVar[MQTTPacketType] = MQTTPacketType.AUTH
    reason_code: int | None = None
    properties: dict[str, Any] = field(default_factory=dict)


# ===========================================================================
# Encoder
# ===========================================================================

_MQTT_PROTOCOL_NAME = 'MQTT'


def _frame(packet_type: MQTTPacketType, flags: int, body: bytes) -> bytes:
    first = (int(packet_type) << 4) | (flags & 0x0F)
    return bytes([first]) + encode_variable_byte_integer(len(body)) + body


def _encode_connect(m: MQTTConnect) -> bytes:
    body = bytearray()
    body += _encode_utf8(_MQTT_PROTOCOL_NAME)
    body.append(m.proto_level)

    flags = 0
    if m.clean_start:
        flags |= ConnectFlags.CLEAN_START
    if m.will_topic is not None:
        flags |= ConnectFlags.WILL_FLAG
        flags |= (m.will_qos & WILL_QOS_MASK) << WILL_QOS_SHIFT
        if m.will_retain:
            flags |= ConnectFlags.WILL_RETAIN
    if m.password is not None:
        flags |= ConnectFlags.PASSWORD
    if m.username is not None:
        flags |= ConnectFlags.USERNAME
    body.append(flags)

    body += int(m.keep_alive).to_bytes(2, 'big')
    # §3.1.2.11 — Properties, and the Will's below, exist from MQTT 5 only, and
    # the decoder below reads the body by the level this same body declares: a
    # pre-v5 CONNECT writes neither, so the else would have nothing to write.
    if m.proto_level >= ProtocolLevel.V5_0:
        body += encode_properties(m.properties)

    body += _encode_utf8(m.client_id)
    if m.will_topic is not None:
        if m.proto_level >= ProtocolLevel.V5_0:
            body += encode_properties(m.will_properties)
        body += _encode_utf8(m.will_topic)
        body += _encode_binary(m.will_payload or b'')
    if m.username is not None:
        body += _encode_utf8(m.username)
    if m.password is not None:
        pw = m.password.encode('utf-8') if isinstance(m.password, str) else m.password
        body += _encode_binary(pw)
    return _frame(MQTTPacketType.CONNECT, 0, bytes(body))


def _encode_connack(m: MQTTConnack) -> bytes:
    body = bytearray()
    body.append(int(m.session_present))
    body.append(int(m.reason_code) & 0xFF)
    body += encode_properties(m.properties)
    return _frame(MQTTPacketType.CONNACK, 0, bytes(body))


def _encode_publish(m: MQTTPublish) -> bytes:
    flags = ((m.qos & PUBLISH_QOS_MASK) << PUBLISH_QOS_SHIFT)
    if m.dup:
        flags |= PublishFlagBits.DUP
    if m.retain:
        flags |= PublishFlagBits.RETAIN
    body = bytearray()
    body += _encode_utf8(m.topic)
    if m.qos > 0:
        # §3.3.2-1: QoS > 0 carries a Packet Identifier.  A missing id defaults
        # to 0 here so header-only round-trips encode; the broker always
        # assigns a real id before sending.
        body += int(m.packet_id or 0).to_bytes(2, 'big')
    body += encode_properties(m.properties)
    body += m.payload
    return _frame(MQTTPacketType.PUBLISH, flags, bytes(body))


def _encode_packet_id_ack(m: _PacketIdAck) -> bytes:
    flags = RESERVED_FLAGS_0010 if m.packet_type == MQTTPacketType.PUBREL else 0
    body = bytearray()
    body += int(m.packet_id).to_bytes(2, 'big')
    body.append(int(m.reason_code) & 0xFF)
    body += encode_properties(m.properties)
    return _frame(m.packet_type, flags, bytes(body))


def _encode_subscribe(m: MQTTSubscribe) -> bytes:
    body = bytearray()
    body += int(m.packet_id).to_bytes(2, 'big')
    body += encode_properties(m.properties)
    for i, (topic_filter, qos) in enumerate(m.subscriptions):
        body += _encode_utf8(topic_filter)
        options = qos & SUBSCRIPTION_QOS_MASK
        if m.subscription_options and i < len(m.subscription_options):
            o = m.subscription_options[i]
            if o.get('no_local'):
                options |= SubscriptionOptions.NO_LOCAL
            if o.get('retain_as_published'):
                options |= SubscriptionOptions.RETAIN_AS_PUBLISHED
            options |= (int(o.get('retain_handling', 0)) & RETAIN_HANDLING_MASK) << RETAIN_HANDLING_SHIFT
        body.append(options)
    return _frame(MQTTPacketType.SUBSCRIBE, RESERVED_FLAGS_0010, bytes(body))


def _encode_suback(m: MQTTSuback) -> bytes:
    body = bytearray()
    body += int(m.packet_id).to_bytes(2, 'big')
    body += encode_properties(m.properties)
    body += bytes(rc & 0xFF for rc in m.reason_codes)
    return _frame(MQTTPacketType.SUBACK, 0, bytes(body))


def _encode_unsubscribe(m: MQTTUnsubscribe) -> bytes:
    body = bytearray()
    body += int(m.packet_id).to_bytes(2, 'big')
    body += encode_properties(m.properties)
    for topic in m.topics:
        body += _encode_utf8(topic)
    return _frame(MQTTPacketType.UNSUBSCRIBE, RESERVED_FLAGS_0010, bytes(body))


def _encode_unsuback(m: MQTTUnsuback) -> bytes:
    body = bytearray()
    body += int(m.packet_id).to_bytes(2, 'big')
    body += encode_properties(m.properties)
    body += bytes(rc & 0xFF for rc in m.reason_codes)
    return _frame(MQTTPacketType.UNSUBACK, 0, bytes(body))


def _encode_reason_and_props(packet_type: MQTTPacketType,
                             reason_code: int | None,
                             properties: dict[str, Any]) -> bytes:
    """DISCONNECT/AUTH: omit the body entirely when reason is absent/0 and no
    properties (§3.14.2.1 / §3.15.2.2)."""
    if reason_code is None and not properties:
        return _frame(packet_type, 0, b'')
    body = bytearray()
    body.append((int(reason_code) if reason_code is not None else 0) & 0xFF)
    body += encode_properties(properties)
    return _frame(packet_type, 0, bytes(body))


# Concrete message class → encoder.  Module-level constant (allocated once at
# import), so encode_packet is a single ``type(message)`` lookup + call rather
# than an O(n) isinstance chain that walks each class's MRO.  The four
# _PacketIdAck subclasses are enumerated explicitly because dict dispatch keys
# on exact type, not base class.
_ENCODERS: dict[type, Callable[[Any], bytes]] = {
    MQTTConnect: _encode_connect,
    MQTTConnack: _encode_connack,
    MQTTPublish: _encode_publish,
    MQTTPuback: _encode_packet_id_ack,
    MQTTPubrec: _encode_packet_id_ack,
    MQTTPubrel: _encode_packet_id_ack,
    MQTTPubcomp: _encode_packet_id_ack,
    MQTTSubscribe: _encode_subscribe,
    MQTTSuback: _encode_suback,
    MQTTUnsubscribe: _encode_unsubscribe,
    MQTTUnsuback: _encode_unsuback,
    MQTTPingreq: lambda m: _frame(MQTTPacketType.PINGREQ, 0, b''),
    MQTTPingresp: lambda m: _frame(MQTTPacketType.PINGRESP, 0, b''),
    MQTTDisconnect: lambda m: _encode_reason_and_props(
        MQTTPacketType.DISCONNECT, m.reason_code, m.properties),
    MQTTAuth: lambda m: _encode_reason_and_props(
        MQTTPacketType.AUTH, m.reason_code, m.properties),
}


def encode_packet(message: MQTTMessage) -> bytes:
    """Serialize an MQTT control packet to its wire representation."""
    encoder = _ENCODERS.get(type(message))
    if encoder is None:
        raise MQTTDecodeError(f'Cannot encode {type(message).__name__}')
    return encoder(message)


# ===========================================================================
# Decoder
# ===========================================================================

def _decode_packet_id(body: bytes, offset: int, packet_type: MQTTPacketType) -> int:
    """§2.2.1 — the two-octet Packet Identifier, which is never zero."""
    if offset + 2 > len(body):
        raise IncompletePacket(f'{packet_type.name} is missing its Packet Identifier')
    packet_id = int.from_bytes(body[offset:offset + 2], 'big')
    if packet_id == 0:
        raise MQTTDecodeError(
            f'{packet_type.name} has Packet Identifier 0 (§2.2.1)')
    return packet_id


def _require_consumed(body: bytes, pos: int, packet_type: MQTTPacketType) -> None:
    """Whatever follows this type's last field is a Malformed Packet."""
    if pos != len(body):
        raise MQTTDecodeError(
            f'{packet_type.name} has {len(body) - pos} octet(s) after its '
            f'last field')


def _decode_without_body(cls: type, body: bytes) -> MQTTMessage:
    """§3.12, §3.13 — PINGREQ and PINGRESP carry no payload."""
    if body:
        raise MQTTDecodeError(
            f'{cls.packet_type.name} carries a {len(body)}-octet payload')
    return cls()


def _decode_connect(body: bytes, flags: int) -> MQTTConnect:
    pos = 0
    _proto_name, pos = _decode_utf8(body, pos)
    # §3.1.2 — Protocol Level (1), Connect Flags (1) and Keep Alive (2) are a
    # fixed 4-byte block after the protocol name.  A CONNECT whose declared
    # Remaining Length stops short of them must be rejected as a Malformed
    # Packet (§1.5.5, §4.13).
    if pos + 4 > len(body):
        raise MQTTDecodeError('CONNECT truncated before the fixed header fields')
    proto_level = body[pos]
    pos += 1
    cflags = body[pos]
    pos += 1
    clean_start = bool(cflags & ConnectFlags.CLEAN_START)
    will_flag = bool(cflags & ConnectFlags.WILL_FLAG)
    will_qos = (cflags >> WILL_QOS_SHIFT) & WILL_QOS_MASK
    will_retain = bool(cflags & ConnectFlags.WILL_RETAIN)
    password_flag = bool(cflags & ConnectFlags.PASSWORD)
    username_flag = bool(cflags & ConnectFlags.USERNAME)
    keep_alive = int.from_bytes(body[pos:pos + 2], 'big')
    pos += 2
    # ProtocolLevel.V3_1_1 and earlier carry no Properties block; only decode
    # one from ProtocolLevel.V5_0.  Lenient decode lets the broker reject an
    # unsupported protocol level with the UNSUPPORTED_PROTOCOL_VERSION CONNACK rather than crash here.
    properties: dict[str, Any] = {}
    if proto_level >= ProtocolLevel.V5_0:
        properties, c = decode_properties(body, pos)
        pos += c

    client_id, pos = _decode_utf8(body, pos)
    will_topic = will_payload = None
    will_properties: dict[str, Any] = {}
    if will_flag:
        if proto_level >= ProtocolLevel.V5_0:
            will_properties, c = decode_properties(body, pos)
            pos += c
        will_topic, pos = _decode_utf8(body, pos)
        will_payload, pos = _decode_binary(body, pos)
    username = None
    if username_flag:
        username, pos = _decode_utf8(body, pos)
    password = None
    if password_flag:
        password, pos = _decode_binary(body, pos)
    _require_consumed(body, pos, MQTTPacketType.CONNECT)

    return MQTTConnect(
        client_id=client_id, clean_start=clean_start, keep_alive=keep_alive,
        proto_level=proto_level, username=username, password=password,
        will_topic=will_topic, will_payload=will_payload, will_qos=will_qos,
        will_retain=will_retain, will_properties=will_properties,
        properties=properties,
    )


def _decode_connack(body: bytes) -> MQTTConnack:
    # §3.2.2 — acknowledge flags, reason code, then a Property Length that is
    # mandatory in MQTT 5 and so one octet at minimum.
    if len(body) < 3:
        raise IncompletePacket('CONNACK is missing its Property Length')
    session_present = bool(body[0] & 0x01)
    reason_code = body[1]
    properties, consumed = decode_properties(body, 2)
    _require_consumed(body, 2 + consumed, MQTTPacketType.CONNACK)
    return MQTTConnack(session_present=session_present, reason_code=reason_code,
                       properties=properties)


def _decode_publish(body: bytes, flags: int) -> MQTTPublish:
    decoded = decode_publish_flags(flags)
    pos = 0
    topic, pos = _decode_utf8(body, pos)
    packet_id = None
    if decoded.qos > 0:
        packet_id = _decode_packet_id(body, pos, MQTTPacketType.PUBLISH)
        pos += 2
    properties, c = decode_properties(body, pos)
    pos += c
    payload = bytes(body[pos:])
    return MQTTPublish(topic=topic, payload=payload, qos=decoded.qos,
                       packet_id=packet_id, retain=decoded.retain,
                       dup=decoded.dup, properties=properties)


def _decode_packet_id_ack(cls: type, body: bytes) -> _PacketIdAck:
    """§3.4-§3.7 — identifier, then an optional reason code and properties.

    A body of exactly two octets is the shortened form (§3.4.2.1) and one of
    exactly three carries the reason code without a Property Length.
    """
    packet_type = cls.packet_type
    packet_id = _decode_packet_id(body, 0, packet_type)
    reason_code = ReasonCode.SUCCESS
    properties: dict[str, Any] = {}
    pos = 2
    if len(body) > 2:
        reason_code = body[2]
        pos = 3
        if len(body) > 3:
            properties, consumed = decode_properties(body, 3)
            pos = 3 + consumed
    _require_consumed(body, pos, packet_type)
    return cls(packet_id=packet_id, reason_code=reason_code, properties=properties)


def _decode_subscribe(body: bytes) -> MQTTSubscribe:
    packet_id = _decode_packet_id(body, 0, MQTTPacketType.SUBSCRIBE)
    properties, c = decode_properties(body, 2)
    pos = 2 + c
    subscriptions: list[tuple[str, int]] = []
    sub_options: list[dict[str, Any]] = []
    while pos < len(body):
        topic_filter, pos = _decode_utf8(body, pos)
        options = body[pos]
        pos += 1
        qos = options & SUBSCRIPTION_QOS_MASK
        subscriptions.append((topic_filter, qos))
        sub_options.append({
            'qos': qos,
            'no_local': bool(options & SubscriptionOptions.NO_LOCAL),
            'retain_as_published': bool(options & SubscriptionOptions.RETAIN_AS_PUBLISHED),
            'retain_handling': (options >> RETAIN_HANDLING_SHIFT) & RETAIN_HANDLING_MASK,
        })
    # §3.8.3 — at least one Topic Filter / Subscription Options pair.
    if not subscriptions:
        raise MQTTDecodeError('SUBSCRIBE has no Topic Filter')
    return MQTTSubscribe(packet_id=packet_id, subscriptions=subscriptions,
                         properties=properties, subscription_options=sub_options)


def _decode_suback(body: bytes) -> MQTTSuback:
    packet_id = _decode_packet_id(body, 0, MQTTPacketType.SUBACK)
    properties, c = decode_properties(body, 2)
    pos = 2 + c
    reason_codes = list(body[pos:])
    # §3.9.3 — at least one Reason Code.
    if not reason_codes:
        raise MQTTDecodeError('SUBACK has no Reason Code')
    return MQTTSuback(packet_id=packet_id, reason_codes=reason_codes,
                      properties=properties)


def _decode_unsubscribe(body: bytes) -> MQTTUnsubscribe:
    packet_id = _decode_packet_id(body, 0, MQTTPacketType.UNSUBSCRIBE)
    properties, c = decode_properties(body, 2)
    pos = 2 + c
    topics: list[str] = []
    while pos < len(body):
        topic, pos = _decode_utf8(body, pos)
        topics.append(topic)
    # §3.10.3 — at least one Topic Filter.
    if not topics:
        raise MQTTDecodeError('UNSUBSCRIBE has no Topic Filter')
    return MQTTUnsubscribe(packet_id=packet_id, topics=topics,
                           properties=properties)


def _decode_unsuback(body: bytes) -> MQTTUnsuback:
    packet_id = _decode_packet_id(body, 0, MQTTPacketType.UNSUBACK)
    properties, c = decode_properties(body, 2)
    pos = 2 + c
    reason_codes = list(body[pos:])
    # §3.11.3 — at least one Reason Code.
    if not reason_codes:
        raise MQTTDecodeError('UNSUBACK has no Reason Code')
    return MQTTUnsuback(packet_id=packet_id, reason_codes=reason_codes,
                        properties=properties)


def _decode_reason_and_props(body: bytes, packet_type: MQTTPacketType
                             ) -> tuple[int | None, dict[str, Any]]:
    """§3.14.2.1, §3.15.2.1 — reason code and properties, both optional."""
    if len(body) == 0:
        return None, {}
    reason_code = body[0]
    properties: dict[str, Any] = {}
    pos = 1
    if len(body) > 1:
        properties, consumed = decode_properties(body, 1)
        pos = 1 + consumed
    _require_consumed(body, pos, packet_type)
    return reason_code, properties


def _decode_reason_props_msg(cls: type, body: bytes) -> MQTTMessage:
    """DISCONNECT/AUTH share a ``reason_code`` + ``properties`` body shape."""
    rc, props = _decode_reason_and_props(body, cls.packet_type)
    return cls(reason_code=rc, properties=props)


# Packet type → decoder, keyed on MQTTPacketType.  Module-level constant: one
# hash + lookup per packet regardless of type, versus the O(n) elif chain's up
# to 17 integer comparisons.  All decoders share a uniform ``(body, flags)``
# signature; those that ignore flags simply don't read the second argument.
_DECODERS: dict[MQTTPacketType, Callable[[bytes, int], MQTTMessage]] = {
    MQTTPacketType.CONNECT:     _decode_connect,
    MQTTPacketType.CONNACK:     lambda body, flags: _decode_connack(body),
    MQTTPacketType.PUBLISH:     _decode_publish,
    MQTTPacketType.PUBACK:      lambda body, flags: _decode_packet_id_ack(MQTTPuback, body),
    MQTTPacketType.PUBREC:      lambda body, flags: _decode_packet_id_ack(MQTTPubrec, body),
    MQTTPacketType.PUBREL:      lambda body, flags: _decode_packet_id_ack(MQTTPubrel, body),
    MQTTPacketType.PUBCOMP:     lambda body, flags: _decode_packet_id_ack(MQTTPubcomp, body),
    MQTTPacketType.SUBSCRIBE:   lambda body, flags: _decode_subscribe(body),
    MQTTPacketType.SUBACK:      lambda body, flags: _decode_suback(body),
    MQTTPacketType.UNSUBSCRIBE: lambda body, flags: _decode_unsubscribe(body),
    MQTTPacketType.UNSUBACK:    lambda body, flags: _decode_unsuback(body),
    MQTTPacketType.PINGREQ:     lambda body, flags: _decode_without_body(MQTTPingreq, body),
    MQTTPacketType.PINGRESP:    lambda body, flags: _decode_without_body(MQTTPingresp, body),
    MQTTPacketType.DISCONNECT:  lambda body, flags: _decode_reason_props_msg(MQTTDisconnect, body),
    MQTTPacketType.AUTH:        lambda body, flags: _decode_reason_props_msg(MQTTAuth, body),
}


def decode_packet(data: bytes) -> MQTTMessage:
    """Decode the first MQTT control packet in *data*.

    Returns the message; it also unpacks into ``(message, bytes_consumed)``.
    Raises [`IncompletePacket`][] if the buffer is short, or
    [`MQTTDecodeError`][] if the bytes are not a valid packet.
    """
    if len(data) < 2:
        raise IncompletePacket('Need at least a 2-byte fixed header')

    first_byte = data[0]
    try:
        packet_type = MQTTPacketType(extract_packet_type(first_byte))
    except ValueError as exc:  # type code 0 — reserved/invalid
        raise MQTTDecodeError(f'Invalid packet type in 0x{first_byte:02X}') from exc
    flags = extract_flags(first_byte)

    # §2.1.3 — reserved fixed-header flag bits.  PUBLISH carries DUP/QoS/RETAIN;
    # PUBREL/SUBSCRIBE/UNSUBSCRIBE MUST be 0b0010; all others MUST be 0b0000.
    # A mismatch is a Malformed Packet.
    if packet_type != MQTTPacketType.PUBLISH:
        expected = RESERVED_FLAGS_0010 if packet_type in (
            MQTTPacketType.PUBREL, MQTTPacketType.SUBSCRIBE,
            MQTTPacketType.UNSUBSCRIBE) else 0
        if flags != expected:
            raise MQTTDecodeError(
                f'Reserved flag bits 0x{flags:X} invalid for {packet_type.name}')

    remaining_length, header_len = _read_vbi_at(data, 1, min(len(data), 5))
    total = header_len + remaining_length
    if total > len(data):
        raise IncompletePacket('Packet body truncated')
    body = data[header_len:total]

    decoder = _DECODERS.get(packet_type)
    if decoder is None:  # pragma: no cover - MQTTPacketType is exhaustive above
        raise MQTTDecodeError(f'Unhandled packet type {packet_type!r}')
    # ``body`` is exactly Remaining Length bytes (validated above), so we hold
    # the whole declared packet.  An inner decoder that still claims "need more"
    # (IncompletePacket) or indexes past the body (IndexError) means the packet's
    # contents are inconsistent with its declared length — a Malformed Packet
    # (§1.5.5, §4.13), not a short read.
    try:
        msg = decoder(body, flags)
    except IncompletePacket as exc:
        raise MQTTDecodeError('Packet body inconsistent with Remaining Length') from exc
    except IndexError as exc:
        raise MQTTDecodeError('Packet body indexed past its Remaining Length') from exc
    except MQTTDecodeError:
        raise
    except ValueError as exc:
        # A message class that refuses what the wire grammar cannot catch
        # (PASSWORD without USERNAME, §3.1.2.9) has still received a Malformed
        # Packet, and a caller catching this codec's errors catches one type.
        raise MQTTDecodeError(str(exc)) from exc

    msg._set_consumed(total)
    return msg


# ===========================================================================
# §4.7 — Topic filter matching
# ===========================================================================

def topic_matches_filter(topic: str, filter_str: str) -> bool:
    """§4.7 — Return True if *topic* matches subscription *filter_str*.

    Handles ``+`` (single level), ``#`` (multi level, terminal), the ``$``
    leading-character rule (§4.7.2), and ``$share/<group>/<filter>`` shared
    subscriptions (§4.8.2).
    """
    if topic == '':
        return False

    # Shared subscription: $share/{ShareName}/{filter} — match against the
    # real filter portion (§4.8.2).
    if filter_str.startswith('$share/'):
        parts = filter_str.split('/', 2)
        if len(parts) < 3:
            return False
        filter_str = parts[2]

    topic_levels = topic.split('/')
    filter_levels = filter_str.split('/')

    # §4.7.2 — wildcards must not match a topic beginning with '$'.
    if topic_levels[0].startswith('$') and filter_levels[0] in ('#', '+'):
        return False

    for i, flevel in enumerate(filter_levels):
        if flevel == '#':
            # Multi-level wildcard matches the parent and all children, but
            # only as the final filter level.
            return i == len(filter_levels) - 1
        if i >= len(topic_levels):
            return False
        if flevel == '+':
            continue
        if flevel != topic_levels[i]:
            return False

    return len(topic_levels) == len(filter_levels)


def validate_topic_name(topic: str) -> bool:
    """§4.7.1 — A Topic *Name* (used in PUBLISH) is literal: non-empty, no
    wildcards (``+``/``#``) and no null character.  Leading/trailing slashes
    are permitted (they denote zero-length levels)."""
    if topic == '':
        return False
    if '\x00' in topic:
        return False
    if '+' in topic or '#' in topic:
        return False
    return True


def validate_topic_filter(filter_str: str) -> bool:
    """§4.7.1 — Validate a subscription Topic *Filter*.

    Returns True when valid; raises ``ValueError`` describing the first
    rule violated.  Enforces single-``#`` / terminal-``#`` / whole-level
    wildcard rules (§4.7.1.2-3) and the ``$share`` share-name rule (§4.8.2).
    """
    if filter_str == '':
        raise ValueError('Topic filter must not be empty')

    if '\x00' in filter_str:
        return False

    work = filter_str
    if filter_str.startswith('$share/'):
        parts = filter_str.split('/', 2)
        if len(parts) < 3 or parts[1] == '':
            raise ValueError('Shared subscription must be $share/{group}/{filter}')
        share_name = parts[1]
        if '+' in share_name or '#' in share_name:
            raise ValueError(
                'Shared subscription share name must not contain wildcards (+ or #)')
        if parts[2] == '':
            raise ValueError(
                'Shared subscription filter portion must not be empty')
        work = parts[2]

    if work.count('#') > 1:
        raise ValueError("Topic filter must contain at most one '#' wildcard")

    levels = work.split('/')
    for i, level in enumerate(levels):
        if '#' in level:
            if level != '#':
                raise ValueError(
                    "'#' must occupy an entire level and be preceded by a slash")
            if i != len(levels) - 1:
                raise ValueError("'#' wildcard must be the last level in a topic filter")
        if '+' in level and level != '+':
            raise ValueError("'+' must occupy an entire level")
    return True
