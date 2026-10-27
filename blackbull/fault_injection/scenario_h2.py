"""HTTP/2 server-side fault scenarios.

WaitForClientFrame match dictionaries accept type, stream_id, flags_set
flags_unset and error_code. JSON Lines supports only ROUND_TRIP_FRAME_CLASSES for
SendFrame; use SendRawBytes for other or malformed frame encodings.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import enum
from typing import Any, ClassVar, Union

from . import _scenario
from ._scenario import Abort, HalfClose, SendRawBytes, Sleep

# A FrameBase subclass — kept loose so this module does not pull in
# the protocol package at import time when the model is being used
# only as data (e.g. catalogue inspection without server start).
Frame = Any


class StepOpH2(str, enum.Enum):
    """The v1 JSON tags, still read until v1 reading is removed."""
    SEND_FRAME = 'SEND_FRAME'
    SEND_RAW = 'SEND_RAW'
    WAIT = 'WAIT'
    EXPECT = 'EXPECT'
    SLEEP = 'SLEEP'
    ABORT = 'ABORT'
    GOAWAY_CLOSE = 'GOAWAY_CLOSE'
    HALF_CLOSE = 'HALF_CLOSE'


def frame_bytes(length: int, type_byte: bytes, flags: int, stream_id: int,
                payload: bytes = b'') -> bytes:
    """RFC 9113 §4.1 — the 9-byte frame header, then *payload*.

    *length* is written as given, so it may disagree with the payload."""
    return (length.to_bytes(3, 'big') + bytes(type_byte)
            + int(flags).to_bytes(1, 'big')
            + (int(stream_id) & 0x7fffffff).to_bytes(4, 'big') + payload)


@dataclass(frozen=True)
class SendFrame:
    """Emit a typed frame; use SendRawBytes for unsupported or malformed encodings.

    A declared_length override changes only the header, not the emitted payload.
    """
    frame: Frame
    declared_length: int | None = None

    def wire(self) -> bytes:
        data = serialize_frame(self.frame)
        if self.declared_length is None:
            return data
        return self.declared_length.to_bytes(3, 'big') + data[3:]


class WaitForClientFrame(_scenario._WaitFor):
    """Consume until a frame matches, skipping nonmatches — harmless on
    HTTP/2, where streams are independent."""


class ExpectClientFrame(_scenario._Expect):
    """Consume exactly one frame and record whether it matched; never filter."""


@dataclass(frozen=True)
class CloseGracefully:
    """Send a GOAWAY then close cleanly.  Terminal, like [`Abort`][].

    ``error_code`` is one of the
    [`ErrorCodes`][blackbull.protocol.frame_types.ErrorCodes] values;
    ``last_stream_id`` advertises the last stream the server is
    willing to process — pass ``0`` to refuse all client streams.
    """
    error_code: int = 0
    last_stream_id: int = 0
    graceful: ClassVar[bool] = True

    def wire(self) -> bytes:
        from blackbull.protocol.frame_types import FrameTypes  # noqa: PLC0415
        return frame_bytes(8, FrameTypes.GOAWAY.value, 0, 0,
                           int(self.last_stream_id).to_bytes(4, 'big')
                           + int(self.error_code).to_bytes(4, 'big'))


# Discriminated union the H2 executor matches on.
H2Step = Union[
    SendFrame,
    SendRawBytes,
    WaitForClientFrame,
    ExpectClientFrame,
    Sleep,
    Abort,
    CloseGracefully,
    HalfClose,
]

# Step is the canonical alias; H2Step remains compatible.
Step = H2Step


@dataclass(frozen=True)
class ScenarioH2:
    """Sequence of steps a programmable H2 server walks per connection.

    Two control knobs sit outside the step list because they apply to
    the whole connection, not to one step:

    * ``send_preface``: whether the server sends its initial SETTINGS at
      handshake time.  Set ``False`` to exercise client behaviour against a
      server that skips the handshake.
    * ``initial_settings``: ``(setting_id, value)`` pairs the server
      advertises in that SETTINGS frame — how the "exhausted window" and
      "illegal SETTINGS" cases inject a hostile starting state.
    """
    steps: tuple[H2Step, ...]
    send_preface: bool = True
    initial_settings: tuple[tuple[int, int], ...] = ()
    name: str = ''

    _v1: ClassVar = (True, {
        StepOpH2.SEND_FRAME: 'SendFrame', StepOpH2.SEND_RAW: 'SendRawBytes',
        StepOpH2.WAIT: 'WaitForClientFrame', StepOpH2.EXPECT: 'ExpectClientFrame',
        StepOpH2.SLEEP: 'Sleep', StepOpH2.ABORT: 'Abort',
        StepOpH2.GOAWAY_CLOSE: 'CloseGracefully', StepOpH2.HALF_CLOSE: 'HalfClose'})


@dataclass
class ScenarioH2Result:
    """Outcome of one [`ScenarioH2`][] run.

    Mirrors [`ScenarioResult`][blackbull.fault_injection.scenario_h1.ScenarioResult]'s
    shape so callers can write uniform assertions across protocols.
    """

    #: Steps that ran to completion.
    steps_completed: int = 0
    #: Total inbound bytes received from the client.
    client_bytes_received: int = 0
    #: Total outbound bytes sent to the client.
    server_bytes_sent: int = 0
    #: ``repr()`` of whatever a step raised; the executor never raises.
    exception: str | None = None
    #: Frames a ``WaitForClientFrame(match=...)`` read and passed over.
    wait_skipped: int = 0
    #: One ``(match, matched)`` pair per ``ExpectClientFrame``, in order.
    expectations: list = field(default_factory=list)
    #: A ``WaitForClientFrame`` or ``ExpectClientFrame`` ran out of time; the
    #: next step still ran, unlike a transport error in ``exception``.
    wait_timed_out: bool = False
    #: An ``Abort`` or ``CloseGracefully`` step ended the scenario.
    terminated: bool = False
    elapsed_s: float = 0.0
    #: True on successful write-side half-close; False if absent or unsupported.
    half_closed: bool = False


# ---------------------------------------------------------------------------
# Match-dict evaluator for WaitForClientFrame
# ---------------------------------------------------------------------------

def _flag_names_set(frame: Frame) -> set[str]:
    """Return the names of flags currently set on *frame*.

    Looks up the flag enum keyed off the frame's ``FRAME_TYPE``.
    Frame-flag enums are uppercase by convention (``END_STREAM``,
    ``END_HEADERS``, ``ACK``), which is what the match dict's
    ``flags_set`` / ``flags_unset`` lists are expected to use.
    """
    flags_int = int(getattr(frame, 'flags', 0) or 0)
    out: set[str] = set()
    # Each FrameBase subclass declares its flag enum via FrameFlags
    # subclassing; we just iterate the enum and mask-test.
    flag_enum = _resolve_flag_enum(frame)
    if flag_enum is None:
        return out
    for member in flag_enum:
        if member.value and (flags_int & member.value) == member.value:
            out.add(member.name)
    return out


def _resolve_flag_enum(frame: Frame):
    """Best-effort lookup of the appropriate flag enum for *frame*."""
    try:
        from blackbull.protocol.frame_types import (
            DataFrameFlags,
            HeaderFrameFlags,
            PingFrameFlags,
            SettingFrameFlags,
        )
    except ImportError:
        return None
    name = type(frame).__name__
    return {
        'Headers': HeaderFrameFlags,
        'Data': DataFrameFlags,
        'SettingFrame': SettingFrameFlags,
        'Ping': PingFrameFlags,
    }.get(name)


def frame_matches(frame: Frame, match: dict) -> bool:
    """Return True iff *frame* satisfies every key in *match*.

    Recognised keys: ``type``, ``stream_id``, ``flags_set``,
    ``flags_unset``, ``error_code``.  Unknown keys fail closed — an
    unrecognised match key is almost certainly a typo in a catalogue
    entry, and silently matching on a missing key would hide the bug.

    ``error_code`` is what turns "a GOAWAY arrived" into "the peer
    rejected this for *that* reason", and a frame carrying no error code
    never matches it.  Both HTTP/2 roles share this function, so the key
    is available to the broken server and the broken client alike.
    """
    recognised = {'type', 'stream_id', 'flags_set', 'flags_unset',
                  'error_code'}
    extra = set(match) - recognised
    if extra:
        return False

    if 'type' in match:
        expected = match['type']
        if type(frame).__name__.upper() != expected.upper() \
                and _frame_type_name(frame) != expected.upper():
            return False

    if 'error_code' in match:
        actual = getattr(frame, 'error_code', None)
        if actual is None or int(actual) != int(match['error_code']):
            return False

    if 'stream_id' in match and match['stream_id'] is not None:
        if getattr(frame, 'stream_id', None) != match['stream_id']:
            return False

    if 'flags_set' in match or 'flags_unset' in match:
        flags = _flag_names_set(frame)
        for required in match.get('flags_set', ()):
            if required not in flags:
                return False
        for forbidden in match.get('flags_unset', ()):
            if forbidden in flags:
                return False

    return True


def _frame_type_name(frame: Frame) -> str:
    """Frame-type name from the FRAME_TYPE registry entry, uppercase."""
    ft = getattr(frame, 'FRAME_TYPE', None)
    if ft is None:
        return type(frame).__name__.upper()
    try:
        # FrameTypes is a bytes-valued enum
        return ft.name
    except AttributeError:
        return type(frame).__name__.upper()


# ---------------------------------------------------------------------------
# The frame record ``SendFrame`` carries in JSON, and its wire bytes
# ---------------------------------------------------------------------------

#: The frame classes the record codec understands.  Both directions and the
#: round-trip test read it, so an arm cannot land on one side alone.
ROUND_TRIP_FRAME_CLASSES = (
    'SettingFrame',
    'WindowUpdate',
    'RstStream',
    'GoAway',
    'Ping',
    'Data',
)

#: Frame classes RFC 9113 places on stream 0; the record cannot state another.
_CONNECTION_LEVEL_FRAMES = ('SettingFrame', 'GoAway', 'Ping')


def _four_octets(value: int, field: str, frame_name: str) -> bytes:
    """Pack *value* as the record states it, or refuse the frame."""
    if not 0 <= value <= 0xffffffff:
        raise TypeError(
            f'{frame_name}.{field} is {value}, which does not fit the four '
            f'octets the record writes it into; use SendRawBytes instead.')
    return value.to_bytes(4, 'big')


def _canonical_payload(frame: Frame) -> bytes:
    """The payload octets the record's fields rebuild for *frame*."""
    name = type(frame).__name__
    if name in ('SettingFrame', 'WindowUpdate', 'Ping', 'Data'):
        return bytes(getattr(frame, 'payload', b'') or b'')
    if name == 'RstStream':
        return _four_octets(int(getattr(frame, 'error_code', 0)),
                            'error_code', name)
    if name == 'GoAway':
        return (_four_octets(int(getattr(frame, 'last_stream_id', 0)),
                             'last_stream_id', name)
                + _four_octets(int(getattr(frame, 'error_code', 0)),
                               'error_code', name)
                + bytes(getattr(frame, 'append_data', b'') or b''))
    raise TypeError(
        f'{name} cannot be round-tripped through scenario_h2 JSON; '
        f'use SendRawBytes instead.')


def require_canonical(frame: Frame) -> None:
    """Refuse a frame the record cannot describe.

    The record carries fields and the executor re-encodes from them, so a
    frame whose type, length, payload or stream identifier they cannot
    reproduce would replay as something other than it is.  Those octets go
    through [`SendRawBytes`][] instead.
    """
    from blackbull.protocol import frame_types  # local; avoids import-time cost

    name = type(frame).__name__
    expected_type = getattr(type(frame), 'FRAME_TYPE', None)
    if expected_type is not None and getattr(frame, 'type_', expected_type) != expected_type:
        raise TypeError(
            f'{name} carries frame type {getattr(frame, "type_", None)!r}; its '
            f'record rebuilds {expected_type!r}; use SendRawBytes instead.')
    payload = _canonical_payload(frame)
    declared = int(getattr(frame, 'length', 0) or 0)
    if declared != len(payload):
        raise TypeError(
            f'{name} declares a {declared}-octet payload but its fields '
            f'describe {len(payload)}; use SendRawBytes instead.')
    if len(payload) > frame_types.MAX_FRAME_SIZE:
        raise TypeError(
            f'{name} carries {len(payload)} payload octets, which exceeds the '
            f'24-bit frame length field (RFC 9113 §4.1); use SendRawBytes '
            f'instead.')
    flags = int(getattr(frame, 'flags', 0) or 0)
    if not 0 <= flags <= 0xff:
        raise TypeError(
            f'{name}.flags is {flags}, which does not fit the octet the record '
            f'writes it into; use SendRawBytes instead.')
    stream_id = int(getattr(frame, 'stream_id', 0) or 0)
    if name in _CONNECTION_LEVEL_FRAMES:
        if stream_id != 0:
            raise TypeError(
                f'{name} is a connection-level frame (RFC 9113); a non-zero '
                f'stream identifier cannot be recorded or replayed; use '
                f'SendRawBytes instead.')
    elif not 0 <= stream_id <= 0x7fffffff:
        raise TypeError(
            f'{name}.stream_id is {stream_id}, which the record cannot put in '
            f'the 31-bit field RFC 9113 §4.1 defines; use SendRawBytes instead.')
    fixed = getattr(type(frame), 'PAYLOAD_LENGTH', None)
    if fixed is not None and len(payload) != fixed:
        raise TypeError(
            f'{name} carries {len(payload)} payload octets, and RFC 9113 fixes '
            f'that at {fixed}; use SendRawBytes instead.')
    if name == 'SettingFrame' and len(payload) % 6:
        raise TypeError(
            f'SettingFrame carries {len(payload)} payload octets, which is not '
            f'a whole number of six-octet entries (RFC 9113 §6.5); use '
            f'SendRawBytes instead.')
    if name == 'Data' and flags & int(frame_types.DataFrameFlags.PADDED):
        raise TypeError(
            'a padded DATA frame keeps its pad length and padding in octets '
            'the record does not carry (RFC 9113 §6.1); use SendRawBytes '
            'instead.')


def _frame_to_dict(frame: Frame) -> dict:
    """Serialise a frame to a round-trippable dict.

    Restricted to `ROUND_TRIP_FRAME_CLASSES`; a frame
    [`require_canonical`][] refuses, and anything outside the list, goes
    through [`SendRawBytes`][] instead.
    """
    require_canonical(frame)
    name = type(frame).__name__
    base = {
        'class': name,
        'stream_id': getattr(frame, 'stream_id', 0),
        'flags': int(getattr(frame, 'flags', 0) or 0),
    }
    if name == 'SettingFrame':
        base['settings'] = [list(pair) for pair in getattr(frame, 'settings', [])]
    elif name == 'WindowUpdate':
        # From the payload, not ``window_size``: the record describes the
        # octets the frame will send.
        base['window_size_increment'] = int.from_bytes(
            bytes(getattr(frame, 'payload', b'') or b''), 'big')
    elif name == 'RstStream':
        base['error_code'] = int(getattr(frame, 'error_code', 0))
    elif name == 'GoAway':
        base['last_stream_id'] = int(getattr(frame, 'last_stream_id', 0))
        base['error_code'] = int(getattr(frame, 'error_code', 0))
        base['append_data'] = bytes(getattr(frame, 'append_data', b'') or b'').hex()
    elif name == 'Ping':
        base['payload'] = bytes(getattr(frame, 'payload', b'') or b'').hex()
    elif name == 'Data':
        base['data'] = bytes(getattr(frame, 'payload', b'') or b'').hex()
    else:
        # Reachable only if a name is added to ROUND_TRIP_FRAME_CLASSES
        # without a writer arm here: the declaration and the codec disagree.
        raise TypeError(f'{name!r} is declared round-trippable but has no writer arm')
    return base


def _four_octets_from_record(d: dict, key: str) -> int:
    """Read a four-octet record field, refusing a value that cannot be one.

    A hand-written record need not come from [`_frame_to_dict`][], and the
    constructor would report the overflow as an ``OverflowError``.
    """
    value = int(d.get(key, 0))
    if not 0 <= value <= 0xffffffff:
        raise ValueError(f'{key}={value} does not fit the four octets it describes')
    return value


def _header_field_from_record(d: dict, key: str, maximum: int) -> int:
    """Read one of the frame header's numeric fields, bounded to *maximum*."""
    value = int(d.get(key, 0))
    if not 0 <= value <= maximum:
        raise ValueError(f'{key}={value} does not fit the field it describes')
    return value


def _payload_from_record(payload: bytes) -> bytes:
    """Refuse a record whose payload the frame length field cannot carry."""
    from blackbull.protocol.frame_types import MAX_FRAME_SIZE

    if len(payload) > MAX_FRAME_SIZE:
        raise ValueError(
            f'a {len(payload)}-octet payload exceeds the 24-bit frame length '
            f'field (RFC 9113 §4.1); use SendRawBytes')
    return payload


def _frame_from_dict(d: dict) -> Frame:
    from blackbull.protocol import frame_types  # local; avoids import-time cost

    name = d['class']
    if name not in ROUND_TRIP_FRAME_CLASSES:
        raise ValueError(f'unknown frame class: {name!r}')
    stream_id = _header_field_from_record(d, 'stream_id', 0x7fffffff)
    flags = _header_field_from_record(d, 'flags', 0xff)
    if name in _CONNECTION_LEVEL_FRAMES and stream_id != 0:
        raise ValueError(
            f'{name} is a connection-level frame (RFC 9113); a non-zero stream '
            f'identifier cannot be recorded or replayed')
    if name == 'SettingFrame':
        # Packed so the frame parses its own payload; its property and
        # ``save()`` then agree with the record.
        entries = [(int(identifier), int(value))
                   for identifier, value in d.get('settings', [])]
        if any(not 0 <= identifier <= 0xffff or not 0 <= value <= 0xffffffff
               for identifier, value in entries):
            raise ValueError('a SETTINGS entry does not fit the field it describes')
        payload = _payload_from_record(b''.join(
            identifier.to_bytes(2, 'big') + value.to_bytes(4, 'big')
            for identifier, value in entries))
        return frame_types.SettingFrame(
            length=len(payload), type_=frame_types.FrameTypes.SETTINGS,
            flags=flags, stream_id=stream_id, data=payload)
    if name == 'WindowUpdate':
        # The increment is the payload, so the frame reads it back as the
        # ``window_size`` the client and server use — one spelling.
        increment = _four_octets_from_record(d, 'window_size_increment')
        return frame_types.WindowUpdate(
            length=4, type_=frame_types.FrameTypes.WINDOW_UPDATE,
            flags=flags, stream_id=stream_id,
            data=increment.to_bytes(4, 'big'))
    if name == 'RstStream':
        # ``RstStream.__init__`` refuses a payload that is not four octets, so
        # the error code arrives as the payload.  ``FrameFactory.rst_stream``
        # would fix ``flags`` to INIT and drop a recorded flag bit.
        error_code = _four_octets_from_record(d, 'error_code')
        return frame_types.RstStream(
            length=4, type_=frame_types.FrameTypes.RST_STREAM,
            flags=flags, stream_id=stream_id,
            data=error_code.to_bytes(4, 'big'))
    if name == 'GoAway':
        # GOAWAY's own encoder writes last-stream-id, error code and any
        # trailing octets, so all three have to be in the payload it parses.
        payload = _payload_from_record(
            _four_octets_from_record(d, 'last_stream_id').to_bytes(4, 'big')
            + _four_octets_from_record(d, 'error_code').to_bytes(4, 'big')
            + bytes.fromhex(d.get('append_data', '')))
        return frame_types.GoAway(
            length=len(payload), type_=frame_types.FrameTypes.GOAWAY,
            flags=flags, stream_id=stream_id, data=payload)
    if name == 'Ping':
        # ``Ping`` alone among the frame classes built here requires
        # ``data``; omitting it makes a serialised PING unreadable back.
        payload = _payload_from_record(bytes.fromhex(d.get('payload', '')))
        f = frame_types.Ping(length=len(payload),
                             type_=frame_types.FrameTypes.PING,
                             flags=flags, stream_id=stream_id, data=payload)
        return f
    if name == 'Data':
        if flags & int(frame_types.DataFrameFlags.PADDED):
            # The record holds the unpadded body, so the pad length and the
            # padding octets are not recoverable; the constructor would take a
            # body octet for the pad length.
            raise ValueError('a padded DATA frame cannot be read back; '
                             'use SendRawBytes')
        payload = _payload_from_record(bytes.fromhex(d.get('data', '')))
        return frame_types.Data(
            length=len(payload), type_=frame_types.FrameTypes.DATA,
            flags=flags, stream_id=stream_id, data=payload)
    # Reachable only if a name is added to ROUND_TRIP_FRAME_CLASSES without a
    # reader arm here: the declaration and the codec disagree.
    raise ValueError(f'{name!r} is declared round-trippable but has no reader arm')


def serialize_frame(frame) -> bytes:
    """Convert a ``FrameBase`` instance to wire bytes.

    Restricted to the classes in `ROUND_TRIP_FRAME_CLASSES`, and to frames
    [`require_canonical`][] accepts; anything else goes through
    [`SendRawBytes`][].
    """
    name = type(frame).__name__
    if name not in ROUND_TRIP_FRAME_CLASSES:
        raise TypeError(
            f'{name} cannot be serialised by H2FaultServer; use SendRawBytes.')
    require_canonical(frame)
    payload = _canonical_payload(frame)
    return frame_bytes(len(payload), type(frame).FRAME_TYPE.value,
                       int(getattr(frame, 'flags', 0) or 0),
                       int(getattr(frame, 'stream_id', 0) or 0), payload)


SendFrame._codecs = {'frame': (_frame_to_dict, _frame_from_dict)}


def scenario_to_json(scenario: ScenarioH2) -> str:
    """Serialise JSON Lines; the HEADER line carries send_preface and initial_settings."""
    return _scenario.to_json(scenario)


def scenario_from_json(src: str) -> ScenarioH2:
    """Parse JSON Lines back to a [`ScenarioH2`][]."""
    return _scenario.from_json(ScenarioH2, src)


__all__ = [
    'Abort',
    'CloseGracefully',
    'HalfClose',
    'Step',
    'ExpectClientFrame',
    'H2Step',
    'ScenarioH2',
    'ScenarioH2Result',
    'SendFrame',
    'SendRawBytes',
    'Sleep',
    'StepOpH2',
    'WaitForClientFrame',
    'frame_matches',
    'frame_bytes',
    'scenario_from_json',
    'scenario_to_json',
    'serialize_frame',
]
