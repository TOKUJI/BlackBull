"""Pure MQTT 5 packet codec, without I/O or broker state.

Decoded messages also unpack as (message, bytes_consumed) for buffer walking.
Packet property and direction rules follow the MQTT 5 OASIS specification.
"""
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
# §1.5 — Field codecs
# ===========================================================================
# Each reader is ``(data, pos, end) -> (value, pos after it)`` and raises
# IncompletePacket when the value would cross *end*.  Inside a packet or a
# property section that is whole already, the caller reports it as malformed.

# §1.5.5 — the continuation bit; a value below it fits one octet.
_VBI_MORE = 0x80


def encode_variable_byte_integer(value: int) -> bytes:
    """§1.5.5 — Encode an int (0..268,435,455) as a Variable Byte Integer."""
    if 0 <= value < _VBI_MORE:
        return bytes((value,))
    if value < 0 or value > 268_435_455:
        raise MQTTDecodeError(f'Variable Byte Integer out of range: {value}')
    out = bytearray()
    while value >= _VBI_MORE:
        out.append(value & 0x7F | 0x80)
        value >>= 7
    out.append(value)
    return bytes(out)


def decode_variable_byte_integer(data: bytes | bytearray, pos: int = 0,
                                 end: int | None = None) -> tuple[int, int]:
    """§1.5.5 — Decode the Variable Byte Integer at *pos*; return
    ``(value, pos after it)``.

    A fourth octet that still continues is malformed.  A non-minimal encoding
    decodes normally: §1.5.5's minimum-bytes rule is not enforced.
    """
    if end is None:
        end = len(data)
    if pos < end and data[pos] < _VBI_MORE:
        return data[pos], pos + 1
    start = pos
    value = 0
    shift = 0
    while pos < end:
        byte = data[pos]
        pos += 1
        value |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return value, pos
        if pos - start == 4:
            raise MQTTDecodeError('Variable Byte Integer too long')
        shift += 7
    raise IncompletePacket('Variable Byte Integer continues past its end')


def _read_u8(data, pos, end):
    if pos >= end:
        raise IncompletePacket('byte past the end')
    return data[pos], pos + 1


def _read_u16(data, pos, end):
    if pos + 2 > end:
        raise IncompletePacket('two-byte integer past the end')
    return data[pos] << 8 | data[pos + 1], pos + 2


def _read_u32(data, pos, end):
    if pos + 4 > end:
        raise IncompletePacket('four-byte integer past the end')
    return (data[pos] << 24 | data[pos + 1] << 16
            | data[pos + 2] << 8 | data[pos + 3]), pos + 4


def _read_binary(data, pos, end):
    if pos + 2 > end:
        raise IncompletePacket('length prefix past the end')
    stop = pos + 2 + (data[pos] << 8 | data[pos + 1])
    if stop > end:
        raise IncompletePacket('data past the end')
    return bytes(data[pos + 2:stop]), stop


def _read_utf8(data, pos, end):
    if pos + 2 > end:
        raise IncompletePacket('length prefix past the end')
    stop = pos + 2 + (data[pos] << 8 | data[pos + 1])
    if stop > end:
        raise IncompletePacket('string past the end')
    return data[pos + 2:stop].decode('utf-8'), stop


def _read_pair(data, pos, end):
    key, pos = _read_utf8(data, pos, end)
    value, pos = _read_utf8(data, pos, end)
    return (key, value), pos


def _encode_utf8(text: str) -> bytes:
    raw = text.encode('utf-8')
    return len(raw).to_bytes(2, 'big') + raw


def _encode_binary(blob: bytes) -> bytes:
    return len(blob).to_bytes(2, 'big') + blob


def _encode_pair(pair: tuple[str, str]) -> bytes:
    key, value = pair
    return _encode_utf8(key) + _encode_utf8(value)


# Wire type -> (reader, writer); writers return the value bytes.
_BYTE, _UINT16, _UINT32, _VBI, _UTF8, _BINARY, _PAIR = (
    'byte', 'uint16', 'uint32', 'vbi', 'utf8', 'binary', 'pair'
)
_CODECS: dict[str, tuple[Callable[[Any, int, int], tuple[Any, int]], Callable[[Any], bytes]]] = {
    _BYTE: (_read_u8, lambda v: bytes((v & 0xFF,))),
    _UINT16: (_read_u16, lambda v: int(v).to_bytes(2, 'big')),
    _UINT32: (_read_u32, lambda v: int(v).to_bytes(4, 'big')),
    _VBI: (decode_variable_byte_integer, lambda v: encode_variable_byte_integer(int(v))),
    _UTF8: (_read_utf8, _encode_utf8),
    _BINARY: (_read_binary, _encode_binary),
    _PAIR: (_read_pair, _encode_pair),
}


# ===========================================================================
# §2.2.2 — Properties
# ===========================================================================

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


# §2.2.2.2 Table 2-3 — each property's wire type.  A message's ``properties``
# dict keys a value by the identifier's lower-case name; User Property, the one
# plural property, collects its (key, value) pairs in order under
# ``user_properties``.
_PROPERTY_TYPES: dict[PropertyId, str] = {
    PropertyId.PAYLOAD_FORMAT_INDICATOR: _BYTE,
    PropertyId.MESSAGE_EXPIRY_INTERVAL: _UINT32,
    PropertyId.CONTENT_TYPE: _UTF8,
    PropertyId.RESPONSE_TOPIC: _UTF8,
    PropertyId.CORRELATION_DATA: _BINARY,
    PropertyId.SUBSCRIPTION_IDENTIFIER: _VBI,
    PropertyId.SESSION_EXPIRY_INTERVAL: _UINT32,
    PropertyId.ASSIGNED_CLIENT_IDENTIFIER: _UTF8,
    PropertyId.SERVER_KEEP_ALIVE: _UINT16,
    PropertyId.AUTHENTICATION_METHOD: _UTF8,
    PropertyId.AUTHENTICATION_DATA: _BINARY,
    PropertyId.REQUEST_PROBLEM_INFORMATION: _BYTE,
    PropertyId.WILL_DELAY_INTERVAL: _UINT32,
    PropertyId.REQUEST_RESPONSE_INFORMATION: _BYTE,
    PropertyId.RESPONSE_INFORMATION: _UTF8,
    PropertyId.SERVER_REFERENCE: _UTF8,
    PropertyId.REASON_STRING: _UTF8,
    PropertyId.RECEIVE_MAXIMUM: _UINT16,
    PropertyId.TOPIC_ALIAS_MAXIMUM: _UINT16,
    PropertyId.TOPIC_ALIAS: _UINT16,
    PropertyId.MAXIMUM_QOS: _BYTE,
    PropertyId.RETAIN_AVAILABLE: _BYTE,
    PropertyId.USER_PROPERTY: _PAIR,
    PropertyId.MAXIMUM_PACKET_SIZE: _UINT32,
    PropertyId.WILDCARD_SUBSCRIPTION_AVAILABLE: _BYTE,
    PropertyId.SUBSCRIPTION_IDENTIFIER_AVAILABLE: _BYTE,
    PropertyId.SHARED_SUBSCRIPTION_AVAILABLE: _BYTE,
}
_PROPERTY_KEYS = {pid: 'user_properties' if wt == _PAIR else pid.name.lower()
                  for pid, wt in _PROPERTY_TYPES.items()}

# User Property is the one plural property: its values aggregate in order.
_PROP_DECODE: dict[int, tuple[str, Callable[[Any, int, int], tuple[Any, int]], bool]] = {
    int(pid): (_PROPERTY_KEYS[pid], _CODECS[wt][0], wt == _PAIR) for pid, wt in _PROPERTY_TYPES.items()
}
_PROP_ENCODE: dict[str, tuple[bytes, Callable[[Any], bytes], bool]] = {
    _PROPERTY_KEYS[pid]: (bytes((pid,)), _CODECS[wt][1], wt == _PAIR)
    for pid, wt in _PROPERTY_TYPES.items()
}

# §2.2.2 — a value is read only inside the declared Property Length; past it
# the packet is malformed, never incomplete.
_CROSSING = 'property value crosses the declared Property Length'


def encode_properties(properties: dict[str, Any]) -> bytes:
    """§2.2.2 — Encode a properties dict to ``Property Length`` + body."""
    body = bytearray()
    for key, value in properties.items():
        spec = _PROP_ENCODE.get(key)
        if spec is None:
            raise MQTTDecodeError(f'Unknown property {key!r}')
        prefix, write, plural = spec
        for item in (value if plural else (value,)):
            body += prefix
            body += write(item)
    return encode_variable_byte_integer(len(body)) + body


def _read_properties(data, pos, end):
    length, pos = decode_variable_byte_integer(data, pos, end)
    stop = pos + length
    if stop > end:
        raise IncompletePacket('Properties body truncated')
    props: dict[str, Any] = {}
    try:
        while pos < stop:
            entry = _PROP_DECODE.get(data[pos])
            if entry is None:
                raise MQTTDecodeError(f'Unknown property identifier 0x{data[pos]:02X}')
            key, read, plural = entry
            value, pos = read(data, pos + 1, stop)
            if plural:
                props.setdefault(key, []).append(value)
            else:
                props[key] = value
    except IncompletePacket as exc:
        raise MQTTDecodeError(_CROSSING) from exc
    return props, stop


def decode_properties(data: bytes) -> tuple[dict[str, Any], int]:
    """§2.2.2 — Decode the properties block *data* starts with; return
    ``(props, consumed)``, the Property Length prefix included."""
    return _read_properties(data, 0, len(data))


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
        yield self._consumed

    def __getitem__(self, index: int):
        # Mirror the ``(message, bytes_consumed)`` tuple shape so callers may
        # also write ``decode_packet(buf)[0]`` / ``[1]``.
        if index == 0:
            return self
        if index == 1:
            return self._consumed
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
    """Subscribe to (filter, max_qos) pairs with a required packet_id.

    subscription_options carries per-entry no_local/retain_as_published/
    retain_handling. Decoding fills it; None on hand-built packets uses defaults.
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
    """Unsubscribe literal registered filters; packet_id is required.
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
# §3 — Packet layouts
# ===========================================================================
# A layout lists one packet type's fields in wire order; encode_packet and
# decode_packet walk it.

class _Field(NamedTuple):
    attr: str
    read: Any
    write: Any

    def encode(self, msg: 'MQTTMessage', body: bytearray) -> None:
        body += self.write(getattr(msg, self.attr))

    def decode(self, data, pos: int, end: int, fields: dict[str, Any]) -> int:
        fields[self.attr], pos = self.read(data, pos, end)
        return pos


class _If(NamedTuple):
    """A field present when *when* holds: a pair of tests, on the message being
    encoded and on the fields decoded so far."""
    when: Any
    attr: str
    read: Any
    write: Any

    def encode(self, msg: 'MQTTMessage', body: bytearray) -> None:
        if self.when[0](msg):
            body += self.write(getattr(msg, self.attr))

    def decode(self, data, pos: int, end: int, fields: dict[str, Any]) -> int:
        if self.when[1](fields):
            fields[self.attr], pos = self.read(data, pos, end)
        return pos


class _Flagged(NamedTuple):
    """§3.1.2.3 — a CONNECT field its flag bit announces: encoded when the
    message's *present* attribute is not None, decoded unless
    [`_ConnectHeader`][] already set it to None."""
    present: str
    attr: str
    read: Any
    write: Any

    def encode(self, msg: 'MQTTMessage', body: bytearray) -> None:
        if getattr(msg, self.present) is not None:
            body += self.write(getattr(msg, self.attr))

    def decode(self, data, pos: int, end: int, fields: dict[str, Any]) -> int:
        if self.attr not in fields:
            fields[self.attr], pos = self.read(data, pos, end)
        return pos


class _Repeated(_Field):
    """Items to the end of the packet; §3.8.3-§3.11.3 require at least one."""
    __slots__ = ()

    def encode(self, msg: 'MQTTMessage', body: bytearray) -> None:
        for item in getattr(msg, self.attr):
            body += self.write(item)

    def decode(self, data, pos: int, end: int, fields: dict[str, Any]) -> int:
        items = []
        while pos < end:
            item, pos = self.read(data, pos, end)
            items.append(item)
        if not items:
            raise MQTTDecodeError(f'no {self.attr}: at least one is required')
        fields[self.attr] = items
        return pos


class _Octets(_Repeated):
    """Reason codes, one octet each."""
    __slots__ = ()

    def encode(self, msg: 'MQTTMessage', body: bytearray) -> None:
        body += bytes(code & 0xFF for code in getattr(msg, self.attr))

    def decode(self, data, pos: int, end: int, fields: dict[str, Any]) -> int:
        if pos >= end:
            raise MQTTDecodeError(f'no {self.attr}: at least one is required')
        fields[self.attr] = list(data[pos:end])
        return end


class _Subscriptions(_Repeated):
    """§3.8.3 — (Topic Filter, Subscription Options) pairs."""
    __slots__ = ()

    def encode(self, msg: 'MQTTMessage', body: bytearray) -> None:
        chosen = msg.subscription_options or ()
        for i, (topic_filter, qos) in enumerate(msg.subscriptions):
            options = qos & SUBSCRIPTION_QOS_MASK
            if i < len(chosen):
                o = chosen[i]
                if o.get('no_local'):
                    options |= _NO_LOCAL
                if o.get('retain_as_published'):
                    options |= _RETAIN_AS_PUBLISHED
                options |= (int(o.get('retain_handling', 0)) & RETAIN_HANDLING_MASK) << RETAIN_HANDLING_SHIFT
            body += _encode_utf8(topic_filter)
            body.append(options)

    def decode(self, data, pos: int, end: int, fields: dict[str, Any]) -> int:
        subscriptions, options = [], []
        while pos < end:
            topic_filter, pos = _read_utf8(data, pos, end)
            o, pos = _read_u8(data, pos, end)
            qos = o & SUBSCRIPTION_QOS_MASK
            subscriptions.append((topic_filter, qos))
            options.append({
                'qos': qos,
                'no_local': bool(o & _NO_LOCAL),
                'retain_as_published': bool(o & _RETAIN_AS_PUBLISHED),
                'retain_handling': o >> RETAIN_HANDLING_SHIFT & RETAIN_HANDLING_MASK,
            })
        if not subscriptions:
            raise MQTTDecodeError('no subscriptions: at least one is required')
        fields['subscriptions'] = subscriptions
        fields['subscription_options'] = options
        return pos


class _ConnectHeader:
    """§3.1.2 — the variable header: protocol name, level, flags byte, keep
    alive and, from MQTT 5, properties.

    The name is written as 'MQTT' and not checked on decode.  A field whose
    flag bit is clear is decoded as its default, None, so the [`_Flagged`][]
    fields after this one read only the fields still missing.
    """
    __slots__ = ()

    def encode(self, msg: 'MQTTMessage', body: bytearray) -> None:
        flags = _CLEAN_START if msg.clean_start else 0
        if msg.will_topic is not None:
            flags |= _WILL | (msg.will_qos & WILL_QOS_MASK) << WILL_QOS_SHIFT
            if msg.will_retain:
                flags |= _WILL_RETAIN
        if msg.password is not None:
            flags |= _PASSWORD
        if msg.username is not None:
            flags |= _USERNAME
        body += _MQTT_PROTOCOL_NAME
        body.append(msg.proto_level)
        body.append(flags)
        body += int(msg.keep_alive).to_bytes(2, 'big')
        if msg.proto_level >= _V5_LEVEL:
            body += encode_properties(msg.properties)

    def decode(self, data, pos: int, end: int, fields: dict[str, Any]) -> int:
        pos = _read_utf8(data, pos, end)[1]
        if pos + 4 > end:
            raise IncompletePacket('CONNECT ends inside its variable header')
        flags = data[pos + 1]
        fields['proto_level'] = data[pos]
        if not flags & _WILL:
            fields['will_topic'] = fields['will_payload'] = None
        if not flags & _USERNAME:
            fields['username'] = None
        if not flags & _PASSWORD:
            fields['password'] = None
        fields['clean_start'] = bool(flags & _CLEAN_START)
        fields['will_qos'] = flags >> WILL_QOS_SHIFT & WILL_QOS_MASK
        fields['will_retain'] = bool(flags & _WILL_RETAIN)
        fields['keep_alive'] = data[pos + 2] << 8 | data[pos + 3]
        pos += 4
        if fields['proto_level'] >= _V5_LEVEL:
            fields['properties'], pos = _read_properties(data, pos, end)
        return pos


class _PublishFlags:
    """§3.3.1 — DUP, QoS and RETAIN in the fixed header; QoS 3 is malformed."""
    __slots__ = ()

    @staticmethod
    def encode(msg: 'MQTTMessage') -> int:
        flags = (msg.qos & PUBLISH_QOS_MASK) << PUBLISH_QOS_SHIFT
        if msg.dup:
            flags |= _DUP
        if msg.retain:
            flags |= _RETAIN
        return flags

    @staticmethod
    def decode(flags: int) -> dict[str, Any]:
        qos = flags >> PUBLISH_QOS_SHIFT & PUBLISH_QOS_MASK
        if qos > 2:
            raise MQTTDecodeError(
                f'PUBLISH with QoS {qos}: both QoS bits set is a Malformed '
                f'Packet (MQTT 5 §3.3.1-4)')
        return {'qos': qos, 'dup': bool(flags & _DUP), 'retain': bool(flags & _RETAIN)}


_MQTT_PROTOCOL_NAME = _encode_utf8('MQTT')
_CLEAN_START, _WILL, _WILL_RETAIN, _PASSWORD, _USERNAME = map(int, (
    ConnectFlags.CLEAN_START, ConnectFlags.WILL_FLAG, ConnectFlags.WILL_RETAIN,
    ConnectFlags.PASSWORD, ConnectFlags.USERNAME))
_DUP, _RETAIN = int(PublishFlagBits.DUP), int(PublishFlagBits.RETAIN)
_NO_LOCAL, _RETAIN_AS_PUBLISHED = map(int, (
    SubscriptionOptions.NO_LOCAL, SubscriptionOptions.RETAIN_AS_PUBLISHED))


def _read_packet_id(data, pos, end):
    """§2.2.1 — never zero."""
    packet_id, pos = _read_u16(data, pos, end)
    if not packet_id:
        raise MQTTDecodeError('Packet Identifier 0 (§2.2.1)')
    return packet_id, pos


def _read_session_present(data, pos, end):
    byte, pos = _read_u8(data, pos, end)
    return bool(byte & 0x01), pos


_TEXT = _CODECS[_UTF8]
_PROPS = (_read_properties, encode_properties)
_PID = (_read_packet_id, lambda v: int(v or 0).to_bytes(2, 'big'))
# An absent reason (DISCONNECT, AUTH) is written as 0 when properties follow.
_REASON = (_read_u8, lambda v: bytes((int(v or 0) & 0xFF,)))
_REST = (lambda data, pos, end: (bytes(data[pos:end]), end), lambda v: v)

_V5_LEVEL = int(ProtocolLevel.V5_0)
_WILL_V5 = (lambda m: m.will_topic is not None and m.proto_level >= _V5_LEVEL,
            lambda f: 'will_topic' not in f and f['proto_level'] >= _V5_LEVEL)
_WITH_QOS = (lambda m: m.qos > 0, lambda f: f['qos'] > 0)

# §3.4.2.1, §3.14.2.1, §3.15.2.1 — read while the packet has bytes left.
_REASON_TAIL = (_Field('reason_code', *_REASON), _Field('properties', *_PROPS))
_PACKET_ID, _PROPERTIES = _Field('packet_id', *_PID), _Field('properties', *_PROPS)


def _nothing_to_say(m: 'MQTTMessage') -> bool:
    """§3.14.2.1, §3.15.2.2 — no reason and no properties: an empty body."""
    return m.reason_code is None and not m.properties


# (class, fixed-header flags (§2.1.3), fields, optional tail, omit the tail when)
_LAYOUTS: tuple[tuple[type, Any, tuple, tuple, Callable[[Any], bool] | None], ...] = (
    (MQTTConnect, 0, (
        _ConnectHeader(),
        _Field('client_id', *_TEXT),
        _If(_WILL_V5, 'will_properties', *_PROPS),
        _Flagged('will_topic', 'will_topic', *_TEXT),
        _Flagged('will_topic', 'will_payload', _read_binary, lambda v: _encode_binary(v or b'')),
        _Flagged('username', 'username', *_TEXT),
        _Flagged('password', 'password', _read_binary, lambda v: _encode_binary(
            v.encode('utf-8') if isinstance(v, str) else v)),
    ), (), None),
    (MQTTConnack, 0, (_Field('session_present', _read_session_present, lambda v: bytes((int(v),))),
                      _Field('reason_code', *_REASON), _PROPERTIES), (), None),
    (MQTTPublish, _PublishFlags, (_Field('topic', *_TEXT), _If(_WITH_QOS, 'packet_id', *_PID),
                                  _PROPERTIES, _Field('payload', *_REST)), (), None),
    (MQTTPuback, 0, (_PACKET_ID,), _REASON_TAIL, None),
    (MQTTPubrec, 0, (_PACKET_ID,), _REASON_TAIL, None),
    (MQTTPubrel, RESERVED_FLAGS_0010, (_PACKET_ID,), _REASON_TAIL, None),
    (MQTTPubcomp, 0, (_PACKET_ID,), _REASON_TAIL, None),
    (MQTTSubscribe, RESERVED_FLAGS_0010,
     (_PACKET_ID, _PROPERTIES, _Subscriptions('subscriptions', None, None)), (), None),
    (MQTTSuback, 0, (_PACKET_ID, _PROPERTIES, _Octets('reason_codes', None, None)), (), None),
    (MQTTUnsubscribe, RESERVED_FLAGS_0010, (_PACKET_ID, _PROPERTIES, _Repeated('topics', *_TEXT)),
     (), None),
    (MQTTUnsuback, 0, (_PACKET_ID, _PROPERTIES, _Octets('reason_codes', None, None)), (), None),
    (MQTTPingreq, 0, (), (), None),
    (MQTTPingresp, 0, (), (), None),
    (MQTTDisconnect, 0, (), _REASON_TAIL, _nothing_to_say),
    (MQTTAuth, 0, (), _REASON_TAIL, _nothing_to_say),
)
# (class, first byte without header flags, fixed flags or None, PUBLISH's header, ...)
_PACKETS = tuple(
    (cls, cls.packet_type << 4, None if flags is _PublishFlags else flags,
     flags if flags is _PublishFlags else None, fields, tail, omit)
    for cls, flags, fields, tail, omit in _LAYOUTS)
_BY_CLASS = {packet[0]: packet for packet in _PACKETS}
_BY_TYPE: list[tuple | None] = [None] * 16
for _packet in _PACKETS:
    _BY_TYPE[_packet[0].packet_type] = _packet


def encode_packet(message: MQTTMessage) -> bytes:
    """Serialize an MQTT control packet to its wire representation."""
    packet = _BY_CLASS.get(type(message))
    if packet is None:
        raise MQTTDecodeError(f'Cannot encode {type(message).__name__}')
    _cls, first, flags, header, fields, tail, omit = packet
    body = bytearray()
    for f in fields:
        f.encode(message, body)
    if tail and (omit is None or not omit(message)):
        for f in tail:
            f.encode(message, body)
    first |= flags if header is None else header.encode(message)
    n = len(body)
    if n < _VBI_MORE:
        return bytes((first, n)) + body
    return bytes((first,)) + encode_variable_byte_integer(n) + body


def encode_short_ack(message: _PacketIdAck) -> bytes | None:
    """PUBACK, PUBREC, PUBREL or PUBCOMP without its reason code and property
    length (§3.4.2.1 and its kin), or ``None`` when it has something to say."""
    if message.reason_code or message.properties:
        return None
    _cls, first, flags, _header, _fields, _tail, _omit = _BY_CLASS[type(message)]
    return bytes((first | flags, 2)) + message.packet_id.to_bytes(2, 'big')


def decode_packet(data: bytes) -> MQTTMessage:
    """Decode the first MQTT control packet in *data*.

    Returns the message; it also unpacks into ``(message, bytes_consumed)``.
    Raises [`IncompletePacket`][] if the buffer is short, or
    [`MQTTDecodeError`][] if the bytes are not a valid packet.
    """
    size = len(data)
    if size < 2:
        raise IncompletePacket('Need at least a 2-byte fixed header')
    first = data[0]
    packet = _BY_TYPE[first >> 4]
    if packet is None:
        raise MQTTDecodeError(f'Reserved packet type 0 in 0x{first:02X} (§2.1.1)')
    cls, _first, flags, header, fields, tail, _omit = packet
    # §2.1.3 — PUBLISH carries DUP/QoS/RETAIN; every other type a fixed value.
    if header is None and first & 0x0F != flags:
        raise MQTTDecodeError(
            f'Reserved flag bits 0x{first & 0x0F:X} invalid for {cls.packet_type.name}')
    if data[1] < _VBI_MORE:
        length, pos = data[1], 2
    else:
        length, pos = decode_variable_byte_integer(data, 1)
    end = pos + length
    if end > size:
        raise IncompletePacket('Packet body truncated')
    # The whole declared packet is here: a field that still wants more bytes,
    # or a class that refuses the values (§3.1.2.9), is a Malformed Packet.
    try:
        decoded = {} if header is None else header.decode(first & 0x0F)
        for f in fields:
            pos = f.decode(data, pos, end, decoded)
        if tail:
            for f in tail:
                if pos == end:
                    break
                pos = f.decode(data, pos, end, decoded)
        if pos != end:
            raise MQTTDecodeError(
                f'{cls.packet_type.name} has {end - pos} octet(s) after its last field')
        msg = cls(**decoded)
    except IncompletePacket as exc:
        raise MQTTDecodeError('Packet body inconsistent with Remaining Length') from exc
    except MQTTDecodeError:
        raise
    except ValueError as exc:
        raise MQTTDecodeError(str(exc)) from exc
    msg._set_consumed(end)
    return msg


# ===========================================================================
# §4.7 — Topic filter matching
# ===========================================================================

def split_shared(topic_filter: str) -> tuple[str, str] | None:
    """§4.8.2 — ``$share/{ShareName}/{filter}`` as ``(share, filter)``; None
    for a filter that is not shared or has no filter portion."""
    if not topic_filter.startswith('$share/'):
        return None
    parts = topic_filter.split('/', 2)
    if len(parts) < 3:
        return None
    return parts[1], parts[2]


def topic_matches_filter(topic: str, filter_str: str) -> bool:
    """§4.7 — Return True if *topic* matches subscription *filter_str*.

    Handles ``+`` (single level), ``#`` (multi level, terminal), the ``$``
    leading-character rule (§4.7.2), and ``$share/<group>/<filter>`` shared
    subscriptions (§4.8.2).
    """
    if topic == '':
        return False

    # §4.8.2 — a shared subscription matches by its filter portion.
    if filter_str.startswith('$share/'):
        shared = split_shared(filter_str)
        if shared is None:
            return False
        filter_str = shared[1]

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
    """Validate MQTT §4.7.1/§4.8.2 wildcard and shared-subscription grammar.

    Return True when valid, False on NUL; raise ValueError for other violations.
    """
    if filter_str == '':
        raise ValueError('Topic filter must not be empty')

    if '\x00' in filter_str:
        return False

    work = filter_str
    if filter_str.startswith('$share/'):
        shared = split_shared(filter_str)
        if shared is None or shared[0] == '':
            raise ValueError('Shared subscription must be $share/{group}/{filter}')
        share_name, work = shared
        if '+' in share_name or '#' in share_name:
            raise ValueError(
                'Shared subscription share name must not contain wildcards (+ or #)')
        if work == '':
            raise ValueError(
                'Shared subscription filter portion must not be empty')

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
