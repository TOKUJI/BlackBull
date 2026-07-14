"""MQTT 5.0 control-packet codec — Sprint 52.

Level-A (pure-data) layer for the ``blackbull-mqtt`` broker sidecar: the 15
MQTT 5.0 control packets as frozen dataclasses, a wire encoder/decoder, the
MQTT 5.0 property system, reason codes, and the topic-filter matching
algorithm.  No I/O and no broker state live here — that is the job of
:mod:`blackbull.mqtt.broker` and :mod:`blackbull.mqtt.connection`.

Reference: MQTT Version 5.0, OASIS Standard
  https://docs.oasis-open.org/mqtt/mqtt/v5.0/os/mqtt-v5.0-os.html

Decoder return contract
-----------------------
:func:`decode_packet` returns the decoded message object.  Every message also
unpacks into ``(message, bytes_consumed)`` so a caller walking a buffer of
concatenated packets can advance its offset::

    msg = decode_packet(buf)            # attribute access / isinstance
    msg, consumed = decode_packet(buf)  # buffer-walking

This dual ergonomics is provided by :meth:`MQTTMessage.__iter__`; the consumed
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
# as a (shift, mask) pair; single bits are :class:`~enum.IntFlag` members.
# ---------------------------------------------------------------------------

class ProtocolLevel(IntEnum):
    """CONNECT Protocol Level (§3.1.2.2).  This broker speaks ``V5_0``."""
    V3_1 = 3
    V3_1_1 = 4
    V5_0 = 5


class ConnectFlags(IntFlag):
    """Single-bit flags in the CONNECT flags byte (§3.1.2.3).

    The Will QoS field is the two-bit subfield at :data:`WILL_QOS_SHIFT`
    (mask :data:`WILL_QOS_MASK`), not a flag here.
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

    QoS is the two-bit subfield at :data:`PUBLISH_QOS_SHIFT`.
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


def decode_publish_flags(flags_byte: int) -> PublishFlags:
    """§3.3.1 — DUP (bit 3), QoS (bits 2-1), RETAIN (bit 0)."""
    return PublishFlags(
        qos=(flags_byte >> PUBLISH_QOS_SHIFT) & PUBLISH_QOS_MASK,
        dup=bool(flags_byte & PublishFlagBits.DUP),
        retain=bool(flags_byte & PublishFlagBits.RETAIN),
    )


# ===========================================================================
# §4 — Reason codes
# ===========================================================================

_REASON_CODE_NAMES: dict[int, str] = {
    0x00: 'Success',
    0x01: 'Granted QoS 1',
    0x02: 'Granted QoS 2',
    0x04: 'Disconnect with Will Message',
    0x10: 'No matching subscribers',
    0x11: 'No subscription existed',
    0x18: 'Continue authentication',
    0x19: 'Re-authenticate',
    0x80: 'Unspecified error',
    0x81: 'Malformed Packet',
    0x82: 'Protocol Error',
    0x83: 'Implementation specific error',
    0x84: 'Unsupported Protocol Version',
    0x85: 'Client Identifier not valid',
    0x86: 'Bad User Name or Password',
    0x87: 'Not authorized',
    0x88: 'Server unavailable',
    0x89: 'Server busy',
    0x8A: 'Banned',
    0x8B: 'Server shutting down',
    0x8C: 'Bad authentication method',
    0x8D: 'Keep Alive timeout',
    0x8E: 'Session taken over',
    0x8F: 'Topic Filter invalid',
    0x90: 'Topic Name invalid',
    0x91: 'Packet Identifier in use',
    0x92: 'Packet Identifier not found',
    0x93: 'Receive Maximum exceeded',
    0x94: 'Topic Alias invalid',
    0x95: 'Packet too large',
    0x96: 'Message rate too high',
    0x97: 'Quota exceeded',
    0x98: 'Administrative action',
    0x99: 'Payload format invalid',
    0x9A: 'Retain not supported',
    0x9B: 'QoS not supported',
    0x9C: 'Use another server',
    0x9D: 'Server moved',
    0x9E: 'Shared Subscriptions not supported',
    0x9F: 'Connection rate exceeded',
    0xA0: 'Maximum connect time',
    0xA1: 'Subscription Identifiers not supported',
    0xA2: 'Wildcard Subscriptions not supported',
}


class MQTTReasonCode(int):
    """An MQTT 5.0 reason code (§2.4).

    A thin ``int`` subclass so any byte value (0-255) is representable without
    raising — undefined codes report ``name == 'Unknown'``.  Codes ``< 0x80``
    are success/normal; ``>= 0x80`` are errors (§2.4).
    """

    def __new__(cls, value: int) -> 'MQTTReasonCode':
        return super().__new__(cls, value)

    @property
    def value(self) -> int:
        return int(self)

    @property
    def name(self) -> str:  # type: ignore[override]
        return _REASON_CODE_NAMES.get(int(self), 'Unknown')

    @property
    def is_success(self) -> bool:
        return int(self) < 0x80

    @property
    def is_error(self) -> bool:
        return int(self) >= 0x80

    def __repr__(self) -> str:
        return f'MQTTReasonCode(0x{int(self):02X}: {self.name})'


class ReasonCode(IntEnum):
    """The subset of §2.4 reason codes the broker references by name.

    This is the single definition of these *values*; their human-readable
    names live once in :data:`_REASON_CODE_NAMES` (the full §2.4 registry used
    by :class:`MQTTReasonCode`).  Importers (`blackbull.mqtt.broker`,
    `blackbull.mqtt.connection`) use these members instead of redeclaring raw
    hex, so a code can never drift between modules.
    """
    SUCCESS = 0x00
    DISCONNECT_WITH_WILL = 0x04
    MALFORMED_PACKET = 0x81
    PROTOCOL_ERROR = 0x82
    UNSUPPORTED_PROTOCOL_VERSION = 0x84
    KEEP_ALIVE_TIMEOUT = 0x8D
    SESSION_TAKEN_OVER = 0x8E
    TOPIC_FILTER_INVALID = 0x8F
    TOPIC_NAME_INVALID = 0x90
    SHARED_SUBSCRIPTIONS_NOT_SUPPORTED = 0x9E


# Guard against the typed subset and the name registry drifting apart: every
# ReasonCode value must be a known §2.4 code.
assert all(int(rc) in _REASON_CODE_NAMES for rc in ReasonCode), \
    'ReasonCode member missing from _REASON_CODE_NAMES (§2.4 registry)'


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


def decode_variable_byte_integer(data: bytes) -> tuple[int, int]:
    """§1.5.5 — Decode a Variable Byte Integer; return ``(value, consumed)``.

    Trailing bytes beyond the integer are ignored (the caller tracks them).
    """
    multiplier = 1
    value = 0
    consumed = 0
    for byte in data:
        value += (byte & 0x7F) * multiplier
        consumed += 1
        if multiplier > 128 * 128 * 128:
            raise MQTTDecodeError('Variable Byte Integer too long')
        if (byte & 0x80) == 0:
            return value, consumed
        multiplier *= 128
    raise IncompletePacket('Variable Byte Integer continues past buffer')


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


class PropertyInfo(NamedTuple):
    """Static description of one MQTT 5.0 property identifier (§2.2.2.2)."""
    identifier: int
    name: str
    wire_type: str


# (id, identifier-name, runtime dict key, wire type).  The identifier name is
# the §2.2.2.2 Table 2-3 name; the runtime key is what appears in a message's
# ``properties`` dict (identical except User Property, which aggregates into
# the plural ``user_properties`` list of (k, v) pairs).
_PROPERTY_SPECS: tuple[tuple[int, str, str, str], ...] = (
    (0x01, 'payload_format_indicator', 'payload_format_indicator', _BYTE),
    (0x02, 'message_expiry_interval', 'message_expiry_interval', _UINT32),
    (0x03, 'content_type', 'content_type', _UTF8),
    (0x08, 'response_topic', 'response_topic', _UTF8),
    (0x09, 'correlation_data', 'correlation_data', _BINARY),
    (0x0B, 'subscription_identifier', 'subscription_identifier', _VBI),
    (0x11, 'session_expiry_interval', 'session_expiry_interval', _UINT32),
    (0x12, 'assigned_client_identifier', 'assigned_client_identifier', _UTF8),
    (0x13, 'server_keep_alive', 'server_keep_alive', _UINT16),
    (0x15, 'authentication_method', 'authentication_method', _UTF8),
    (0x16, 'authentication_data', 'authentication_data', _BINARY),
    (0x17, 'request_problem_information', 'request_problem_information', _BYTE),
    (0x18, 'will_delay_interval', 'will_delay_interval', _UINT32),
    (0x19, 'request_response_information', 'request_response_information', _BYTE),
    (0x1A, 'response_information', 'response_information', _UTF8),
    (0x1C, 'server_reference', 'server_reference', _UTF8),
    (0x1F, 'reason_string', 'reason_string', _UTF8),
    (0x21, 'receive_maximum', 'receive_maximum', _UINT16),
    (0x22, 'topic_alias_maximum', 'topic_alias_maximum', _UINT16),
    (0x23, 'topic_alias', 'topic_alias', _UINT16),
    (0x24, 'maximum_qos', 'maximum_qos', _BYTE),
    (0x25, 'retain_available', 'retain_available', _BYTE),
    (0x26, 'user_property', 'user_properties', _PAIR),
    (0x27, 'maximum_packet_size', 'maximum_packet_size', _UINT32),
    (0x28, 'wildcard_subscription_available', 'wildcard_subscription_available', _BYTE),
    (0x29, 'subscription_identifier_available', 'subscription_identifier_available', _BYTE),
    (0x2A, 'shared_subscription_available', 'shared_subscription_available', _BYTE),
)

# §2.2.2.2 Table 2-3 — {identifier: name}.  Exactly 27 entries.
PROPERTY_IDENTIFIERS: dict[int, str] = {
    pid: ident for pid, ident, _key, _wt in _PROPERTY_SPECS
}

_PROP_BY_ID: dict[int, PropertyInfo] = {
    pid: PropertyInfo(pid, ident, wt) for pid, ident, _key, wt in _PROPERTY_SPECS
}
_PROP_BY_KEY: dict[str, tuple[int, str]] = {
    key: (pid, wt) for pid, _ident, key, wt in _PROPERTY_SPECS
}
_PROP_ID_TO_KEY: dict[int, str] = {
    pid: key for pid, _ident, key, _wt in _PROPERTY_SPECS
}


def get_property_info(identifier: int) -> PropertyInfo | None:
    """Return the :class:`PropertyInfo` for a property identifier, or None."""
    return _PROP_BY_ID.get(identifier)


def _decode_vbi_at(data: bytes, pos: int, end: int) -> tuple[int, int]:
    val, consumed = decode_variable_byte_integer(data[pos:end])
    return val, pos + consumed


# Wire type → (value encoder, value decoder).  Module-level constant, so both
# property codec paths are a single dict lookup instead of a 6-branch chain
# (the original ``_encode_prop_value`` used bare ``if`` — not ``elif`` — so every
# branch was evaluated even after a match).  Encoders take the raw value and
# return the encoded bytes *without* the property-id prefix; decoders share a
# uniform ``(data, pos, end) -> (value, new_pos)`` signature.  ``_PAIR`` (user
# properties) is handled by guard clauses in both callers, not via this table.
_WIRE_CODECS: dict[str, tuple[Callable[[Any], bytes],
                              Callable[[bytes, int, int], tuple[Any, int]]]] = {
    _BYTE:   (lambda v: bytes([v & 0xFF]),
              lambda d, p, e: (d[p], p + 1)),
    _UINT16: (lambda v: int(v).to_bytes(2, 'big'),
              lambda d, p, e: (int.from_bytes(d[p:p + 2], 'big'), p + 2)),
    _UINT32: (lambda v: int(v).to_bytes(4, 'big'),
              lambda d, p, e: (int.from_bytes(d[p:p + 4], 'big'), p + 4)),
    _VBI:    (lambda v: encode_variable_byte_integer(int(v)), _decode_vbi_at),
    _UTF8:   (_encode_utf8, lambda d, p, e: _decode_utf8(d, p)),
    _BINARY: (_encode_binary, lambda d, p, e: _decode_binary(d, p)),
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
    length, len_consumed = decode_variable_byte_integer(data[offset:])
    start = offset + len_consumed
    end = start + length
    if end > len(data):
        raise IncompletePacket('Properties body truncated')
    props: dict[str, Any] = {}
    pos = start
    while pos < end:
        pid = data[pos]
        pos += 1
        spec = _PROP_BY_ID.get(pid)
        if spec is None:
            raise MQTTDecodeError(f'Unknown property identifier 0x{pid:02X}')
        wire_type = spec.wire_type
        runtime_key = _PROP_ID_TO_KEY[pid]
        if wire_type == _PAIR:
            pair_key, pos = _decode_utf8(data, pos)
            pair_val, pos = _decode_utf8(data, pos)
            props.setdefault(runtime_key, []).append((pair_key, pair_val))
            continue
        codec = _WIRE_CODECS.get(wire_type)
        if codec is None:  # pragma: no cover - exhaustive above
            raise MQTTDecodeError(f'Unhandled property wire type {wire_type!r}')
        props[runtime_key], pos = codec[1](data, pos, end)
    return props, end - offset


# ===========================================================================
# Message dataclasses
# ===========================================================================

class MQTTMessage:
    """Base for all MQTT control-packet dataclasses.

    Provides the dual decode contract: a decoded message unpacks into
    ``(message, bytes_consumed)``.  The byte count is set by
    :func:`decode_packet` via :meth:`_set_consumed`; messages built by hand
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
    packet_type: ClassVar[MQTTPacketType] = MQTTPacketType.CONNECT
    client_id: str
    clean_start: bool
    keep_alive: int
    proto_level: int = 5
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
    packet_type: ClassVar[MQTTPacketType] = MQTTPacketType.CONNACK
    session_present: bool = False
    reason_code: int = 0
    properties: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class MQTTPublish(MQTTMessage):
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
    reason_code: int = 0
    properties: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class MQTTPuback(_PacketIdAck):
    packet_type: ClassVar[MQTTPacketType] = MQTTPacketType.PUBACK


@dataclass(frozen=True)
class MQTTPubrec(_PacketIdAck):
    packet_type: ClassVar[MQTTPacketType] = MQTTPacketType.PUBREC


@dataclass(frozen=True)
class MQTTPubrel(_PacketIdAck):
    packet_type: ClassVar[MQTTPacketType] = MQTTPacketType.PUBREL


@dataclass(frozen=True)
class MQTTPubcomp(_PacketIdAck):
    packet_type: ClassVar[MQTTPacketType] = MQTTPacketType.PUBCOMP


@dataclass(frozen=True)
class MQTTSubscribe(MQTTMessage):
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
    packet_type: ClassVar[MQTTPacketType] = MQTTPacketType.SUBACK
    packet_id: int
    reason_codes: list[int]
    properties: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class MQTTUnsubscribe(MQTTMessage):
    packet_type: ClassVar[MQTTPacketType] = MQTTPacketType.UNSUBSCRIBE
    packet_id: int | None = None
    topics: list[str] = field(default_factory=list)
    properties: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.packet_id is None:
            raise ValueError('UNSUBSCRIBE requires a Packet Identifier (§3.10.2)')


@dataclass(frozen=True)
class MQTTUnsuback(MQTTMessage):
    packet_type: ClassVar[MQTTPacketType] = MQTTPacketType.UNSUBACK
    packet_id: int
    reason_codes: list[int]
    properties: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class MQTTPingreq(MQTTMessage):
    packet_type: ClassVar[MQTTPacketType] = MQTTPacketType.PINGREQ


@dataclass(frozen=True)
class MQTTPingresp(MQTTMessage):
    packet_type: ClassVar[MQTTPacketType] = MQTTPacketType.PINGRESP


@dataclass(frozen=True)
class MQTTDisconnect(MQTTMessage):
    packet_type: ClassVar[MQTTPacketType] = MQTTPacketType.DISCONNECT
    reason_code: int | None = None
    properties: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class MQTTAuth(MQTTMessage):
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
    body += encode_properties(m.properties)

    body += _encode_utf8(m.client_id)
    if m.will_topic is not None:
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
    body.append(0x01 if m.session_present else 0x00)
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
    flags = RESERVED_FLAGS_0010 if m.packet_type == MQTTPacketType.PUBREL else 0x00
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

def _decode_connect(body: bytes, flags: int) -> MQTTConnect:
    pos = 0
    _proto_name, pos = _decode_utf8(body, pos)
    # §3.1.2 — Protocol Level (1), Connect Flags (1) and Keep Alive (2) are a
    # fixed 4-byte block after the protocol name.  A CONNECT whose declared
    # Remaining Length stops short of them must be rejected as a Malformed
    # Packet (§1.5.5, §4.13) — a raw ``body[pos]`` here would raise IndexError,
    # which the framer's ``except`` does not catch, unwinding ``read_loop``
    # and dropping the connection instead of resyncing (audit 1.19g).
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
    # MQTT 3.1.1 (proto_level 4) and earlier carry no Properties block; only
    # decode one for MQTT 5.0.  Lenient decode lets the broker reject an
    # unsupported protocol level with CONNACK 0x84 rather than crash here.
    properties: dict[str, Any] = {}
    if proto_level >= 5:
        properties, c = decode_properties(body, pos)
        pos += c

    client_id, pos = _decode_utf8(body, pos)
    will_topic = will_payload = None
    will_properties: dict[str, Any] = {}
    if will_flag:
        if proto_level >= 5:
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

    return MQTTConnect(
        client_id=client_id, clean_start=clean_start, keep_alive=keep_alive,
        proto_level=proto_level, username=username, password=password,
        will_topic=will_topic, will_payload=will_payload, will_qos=will_qos,
        will_retain=will_retain, will_properties=will_properties,
        properties=properties,
    )


def _decode_connack(body: bytes) -> MQTTConnack:
    session_present = bool(body[0] & 0x01)
    reason_code = body[1]
    properties, _ = decode_properties(body, 2)
    return MQTTConnack(session_present=session_present, reason_code=reason_code,
                       properties=properties)


def _decode_publish(body: bytes, flags: int) -> MQTTPublish:
    decoded = decode_publish_flags(flags)
    pos = 0
    topic, pos = _decode_utf8(body, pos)
    packet_id = None
    if decoded.qos > 0:
        packet_id = int.from_bytes(body[pos:pos + 2], 'big')
        pos += 2
    properties, c = decode_properties(body, pos)
    pos += c
    payload = bytes(body[pos:])
    return MQTTPublish(topic=topic, payload=payload, qos=decoded.qos,
                       packet_id=packet_id, retain=decoded.retain,
                       dup=decoded.dup, properties=properties)


def _decode_packet_id_ack(cls: type, body: bytes) -> _PacketIdAck:
    packet_id = int.from_bytes(body[0:2], 'big')
    reason_code = 0
    properties: dict[str, Any] = {}
    if len(body) > 2:
        reason_code = body[2]
        if len(body) > 3:
            properties, _ = decode_properties(body, 3)
    return cls(packet_id=packet_id, reason_code=reason_code, properties=properties)


def _decode_subscribe(body: bytes) -> MQTTSubscribe:
    packet_id = int.from_bytes(body[0:2], 'big')
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
    return MQTTSubscribe(packet_id=packet_id, subscriptions=subscriptions,
                         properties=properties, subscription_options=sub_options)


def _decode_suback(body: bytes) -> MQTTSuback:
    packet_id = int.from_bytes(body[0:2], 'big')
    properties, c = decode_properties(body, 2)
    pos = 2 + c
    reason_codes = list(body[pos:])
    return MQTTSuback(packet_id=packet_id, reason_codes=reason_codes,
                      properties=properties)


def _decode_unsubscribe(body: bytes) -> MQTTUnsubscribe:
    packet_id = int.from_bytes(body[0:2], 'big')
    properties, c = decode_properties(body, 2)
    pos = 2 + c
    topics: list[str] = []
    while pos < len(body):
        topic, pos = _decode_utf8(body, pos)
        topics.append(topic)
    return MQTTUnsubscribe(packet_id=packet_id, topics=topics,
                           properties=properties)


def _decode_unsuback(body: bytes) -> MQTTUnsuback:
    packet_id = int.from_bytes(body[0:2], 'big')
    properties, c = decode_properties(body, 2)
    pos = 2 + c
    reason_codes = list(body[pos:])
    return MQTTUnsuback(packet_id=packet_id, reason_codes=reason_codes,
                        properties=properties)


def _decode_reason_and_props(body: bytes) -> tuple[int | None, dict[str, Any]]:
    if len(body) == 0:
        return None, {}
    reason_code = body[0]
    properties: dict[str, Any] = {}
    if len(body) > 1:
        properties, _ = decode_properties(body, 1)
    return reason_code, properties


def _decode_reason_props_msg(cls: type, body: bytes) -> MQTTMessage:
    """DISCONNECT/AUTH share a ``reason_code`` + ``properties`` body shape."""
    rc, props = _decode_reason_and_props(body)
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
    MQTTPacketType.PINGREQ:     lambda body, flags: MQTTPingreq(),
    MQTTPacketType.PINGRESP:    lambda body, flags: MQTTPingresp(),
    MQTTPacketType.DISCONNECT:  lambda body, flags: _decode_reason_props_msg(MQTTDisconnect, body),
    MQTTPacketType.AUTH:        lambda body, flags: _decode_reason_props_msg(MQTTAuth, body),
}


def decode_packet(data: bytes) -> MQTTMessage:
    """Decode the first MQTT control packet in *data*.

    Returns the message; it also unpacks into ``(message, bytes_consumed)``.
    Raises :class:`IncompletePacket` if the buffer is short, or
    :class:`MQTTDecodeError` if the bytes are not a valid packet.
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
    # A mismatch is a Malformed Packet — also the signal the actor's resync uses
    # to skip junk bytes.
    if packet_type != MQTTPacketType.PUBLISH:
        expected = RESERVED_FLAGS_0010 if packet_type in (
            MQTTPacketType.PUBREL, MQTTPacketType.SUBSCRIBE,
            MQTTPacketType.UNSUBSCRIBE) else 0x00
        if flags != expected:
            raise MQTTDecodeError(
                f'Reserved flag bits 0x{flags:X} invalid for {packet_type.name}')

    remaining_length, rl_consumed = decode_variable_byte_integer(data[1:])
    header_len = 1 + rl_consumed
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
    # (§1.5.5, §4.13), not a short read.  Both must surface as MQTTDecodeError so
    # the framer resyncs (drops a byte) instead of stalling forever on a body
    # that will never grow, or unwinding read_loop on an uncaught IndexError.
    try:
        msg = decoder(body, flags)
    except IncompletePacket as exc:
        raise MQTTDecodeError('Packet body inconsistent with Remaining Length') from exc
    except IndexError as exc:
        raise MQTTDecodeError('Packet body indexed past its Remaining Length') from exc

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

    Returns True when valid; raises :class:`ValueError` describing the first
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
