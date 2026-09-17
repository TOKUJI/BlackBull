"""Programmable HTTP/2 wire-level scenario model.

A [`ScenarioH2`][] is a sequence of typed *steps* that the
[`blackbull.fault_injection.h2_server.H2FaultServer`][blackbull.fault_injection.h2_server.H2FaultServer] executor
walks in order against a connected HTTP/2 client.  This is the
*server-side* half of the [`blackbull.fault_injection`][blackbull.fault_injection] toolkit:
a programmable server that emits deliberate misbehaviour toward a
client — half-closed streams, exhausted flow-control windows,
illegal SETTINGS, weird frame sequences — expressed as data, not
procedural test code.

The symmetric *client-side* half (programmable HTTP/1.1 client
driving deliberate misbehaviour toward a server) lives in
[`blackbull.fault_injection.scenario_h1`][blackbull.fault_injection.scenario_h1].

Use cases:

  * HTTP/2 client-library authors testing their client's resilience
    against a misbehaving server.
  * Proxy / load-balancer authors testing what their transit code
    does when an upstream emits illegal frame sequences.
  * Security researchers reproducing CVE-class patterns
    (CONTINUATION-flood, RST-flood) from a deterministic harness.

Steps
-----

* [`SendFrame`][] — emit one parsed ``FrameBase`` instance.
  The executor handles serialisation through the existing
  [`FrameFactory`][blackbull.protocol.frame.FrameFactory].
* [`SendRawBytes`][] — escape hatch for bytes the framework's
  ``FrameFactory`` cannot construct (e.g. illegal frame types,
  oversized frames, malformed length fields).
* [`WaitForClientFrame`][] — pause until an inbound frame from
  the client matches the *declarative match dict*.  Fields supported:

  ===============  =========================================
  ``type``         ``'HEADERS'``, ``'SETTINGS'``, etc.
  ``stream_id``    Integer; ``None`` = any.
  ``flags_set``    List of flag names (uppercase) that must be set.
  ``flags_unset``  List of flag names that must be unset.
  ===============  =========================================

* [`Sleep`][] — idle without sending or reading.
* [`Abort`][] — hard-close the underlying transport (RST on
  Linux).
* [`CloseGracefully`][] — send a GOAWAY frame, then close
  cleanly.

Serialisation
-------------

[`scenario_to_json`][] / [`scenario_from_json`][] round-trip
through JSON Lines, so a [`SendFrame`][] frame has to be
reconstructable from its serialised form — the classes in
[`ROUND_TRIP_FRAME_CLASSES`][].  Everything else, a padded DATA
frame included, goes through [`SendRawBytes`][], which always
round-trips.
"""
from __future__ import annotations

import base64
import enum
import json
from dataclasses import dataclass, field
from typing import Any, Union

# A FrameBase subclass — kept loose so this module does not pull in
# the protocol package at import time when the model is being used
# only as data (e.g. catalogue inspection without server start).
Frame = Any


class StepOpH2(str, enum.Enum):
    """Tag used by the JSON serialiser."""
    SEND_FRAME = 'SEND_FRAME'
    SEND_RAW = 'SEND_RAW'
    WAIT = 'WAIT'
    EXPECT = 'EXPECT'
    SLEEP = 'SLEEP'
    ABORT = 'ABORT'
    GOAWAY_CLOSE = 'GOAWAY_CLOSE'
    HALF_CLOSE = 'HALF_CLOSE'


@dataclass(frozen=True)
class SendFrame:
    """Emit one parsed frame onto the connection.

    Routed through [`FrameFactory`][blackbull.protocol.frame.FrameFactory] so
    the on-wire serialisation matches the framework's normal output.
    Use [`SendRawBytes`][] for frames the factory cannot construct.

    ``declared_length`` overrides the header's length field without
    changing the bytes actually written — "the peer lied about how much is
    coming", which a serialiser that computes the length cannot say.
    Leave it ``None`` (the default) and nothing changes.
    """
    frame: Frame
    declared_length: int | None = None


@dataclass(frozen=True)
class SendRawBytes:
    """Push arbitrary bytes onto the connection.

    Escape hatch for malformed frames (illegal type byte, length
    exceeding ``SETTINGS_MAX_FRAME_SIZE``, etc.) that the typed
    [`SendFrame`][] path will not produce.

    ``byte_interval > 0`` transmits one byte at a time with that
    delay — useful for stalled-handshake patterns where the client
    is expected to enforce a preface-completion timeout.
    """
    data: bytes
    byte_interval: float = 0.0


@dataclass(frozen=True)
class WaitForClientFrame:
    """Block until an inbound frame matches ``match``.

    Declarative grammar — see module docstring for the supported
    keys.  Frames the client sends that do *not* match are still
    consumed (the executor remains responsive to the wire) but do
    not advance this step.

    On ``timeout`` expiry the executor records the miss on
    [`ScenarioH2Result`][] and proceeds to the next step.
    """
    match: dict = field(default_factory=dict)
    timeout: float = 5.0


@dataclass(frozen=True)
class ExpectClientFrame:
    """Read one inbound frame and record whether it matched.

    A guard, not a filter: nothing is skipped and the executor moves on
    either way.  It answers a different question from
    [`WaitForClientFrame`][] — *is the client under test behaving as
    this scenario assumes?* — and a scenario whose premise silently failed
    would otherwise look like a pass.

    The HTTP/1.1 half has the same pair for the same reason
    (``ExpectRequest``); the names differ only where the unit does.
    """
    match: dict = field(default_factory=dict)
    timeout: float = 5.0


@dataclass(frozen=True)
class Sleep:
    """Idle for ``duration`` seconds without reading or writing."""
    duration: float


@dataclass(frozen=True)
class Abort:
    """Hard-close the connection (transport.abort → RST on Linux)."""


@dataclass(frozen=True)
class CloseGracefully:
    """Send a GOAWAY then close cleanly.

    Subsequent scenario steps short-circuit (this is a terminator
    just like [`Abort`][]).  ``error_code`` is one of the
    [`ErrorCodes`][blackbull.protocol.frame_types.ErrorCodes] values;
    ``last_stream_id`` advertises the last stream the server is
    willing to process — pass ``0`` to refuse all client streams,
    or the highest accepted stream ID otherwise.
    """
    error_code: int = 0
    last_stream_id: int = 0


@dataclass(frozen=True)
class HalfClose:
    """Shut down the sending direction only (FIN), keep reading.

    **Not terminal** — later steps still run, which is the whole point.
    ``Abort`` is not a substitute: it sends RST, discarding what is buffered
    and leaving nothing to read.
    """


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

#: The name the other three vocabularies use.  ``H2Step`` stays for the
#: callers that already import it; new code should read ``Step``, so a
#: reader comparing the four files is not told they differ where they do
#: not.
Step = H2Step


@dataclass(frozen=True)
class ScenarioH2:
    """Sequence of steps a programmable H2 server walks per connection.

    Two control knobs sit outside the step list because they apply to
    the whole connection, not to one step:

    * ``send_preface``: whether the server sends the standard
      ``SERVER_PREFACE_BYTES`` + initial SETTINGS at handshake time.
      Most catalogue scenarios want this (real H2 clients require it
      before proceeding); set ``False`` to exercise client behaviour
      against a server that skips the handshake.
    * ``initial_settings``: tuple of ``(setting_id, value)`` pairs the
      server advertises in its initial SETTINGS frame.  Used by the
      "exhausted window" and "illegal SETTINGS" catalogue entries to
      inject a hostile starting state before the first step runs.
    """
    steps: tuple[H2Step, ...]
    send_preface: bool = True
    initial_settings: tuple[tuple[int, int], ...] = ()
    #: For test parametrisation and for the JSON header line, as
    #: [`ScenarioH1Server`][blackbull.fault_injection.scenario_h1_server.ScenarioH1Server]
    #: has.  A scenario that can be reported on by name is one a failing CI
    #: run can point at.
    name: str = ''


@dataclass
class ScenarioH2Result:
    """Outcome of one [`ScenarioH2`][] run.

    Mirrors [`ScenarioResult`][blackbull.fault_injection.scenario_h1.ScenarioResult]'s
    shape so callers can write uniform pytest assertions across
    protocols.
    """

    # 0-based count of steps that ran to completion (excluding the
    # step that aborted, errored, or fell out of timeout).
    steps_completed: int = 0

    # Total inbound bytes received from the client.
    client_bytes_received: int = 0

    # Total outbound bytes sent to the client.
    server_bytes_sent: int = 0

    # If a step raised, this is the repr.  The executor never lets a
    # scenario bubble exceptions to the caller.
    exception: str | None = None

    # Frames a WaitForClientFrame(match=...) step read and passed over.
    # Harmless here — HTTP/2 streams are independent — where the same
    # count on HTTP/1.1 means the connection is desynced.
    wait_skipped: int = 0

    # One (match, matched) pair per ExpectClientFrame step, in order:
    # what the scenario assumed, and whether it held.
    expectations: list = field(default_factory=list)

    # Whether a WaitForClientFrame step timed out before its match
    # arrived.  When True the step still counts as completed and the
    # next step runs; this distinguishes a per-step timeout (recorded
    # here) from a transport-level error (recorded in ``exception``).
    wait_timed_out: bool = False

    # True when execution stopped because an Abort or CloseGracefully
    # step ran.
    terminated: bool = False

    elapsed_s: float = 0.0
    #: True when a ``HalfClose`` step actually shut down the write side.
    #: False both when no such step ran and when the transport refused it
    #: (TLS has no half-close), so a test can tell "did not ask" from
    #: "asked and it did not happen" — a silently skipped half-close
    #: otherwise reads as a pass.
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
# JSON Lines serialisation
# ---------------------------------------------------------------------------

def _step_to_dict(step: H2Step) -> dict:
    if isinstance(step, SendFrame):
        return {
            'op': StepOpH2.SEND_FRAME.value,
            'frame': _frame_to_dict(step.frame),
            'declared_length': step.declared_length,
        }
    if isinstance(step, SendRawBytes):
        return {
            'op': StepOpH2.SEND_RAW.value,
            'data': base64.b64encode(step.data).decode('ascii'),
            'byte_interval': step.byte_interval,
        }
    if isinstance(step, ExpectClientFrame):
        return {'op': StepOpH2.EXPECT.value, 'match': dict(step.match),
                'timeout': step.timeout}
    if isinstance(step, WaitForClientFrame):
        return {
            'op': StepOpH2.WAIT.value,
            'match': dict(step.match),
            'timeout': step.timeout,
        }
    if isinstance(step, Sleep):
        return {'op': StepOpH2.SLEEP.value, 'duration': step.duration}
    if isinstance(step, Abort):
        return {'op': StepOpH2.ABORT.value}
    if isinstance(step, HalfClose):
        return {'op': StepOpH2.HALF_CLOSE.value}
    if isinstance(step, CloseGracefully):
        return {
            'op': StepOpH2.GOAWAY_CLOSE.value,
            'error_code': step.error_code,
            'last_stream_id': step.last_stream_id,
        }
    raise TypeError(f'unknown step type: {type(step).__name__}')


def _step_from_dict(d: dict) -> H2Step:
    op = d.get('op')
    if op == StepOpH2.SEND_FRAME.value:
        return SendFrame(frame=_frame_from_dict(d['frame']),
                         declared_length=d.get('declared_length'))
    if op == StepOpH2.SEND_RAW.value:
        return SendRawBytes(
            data=base64.b64decode(d['data']),
            byte_interval=float(d.get('byte_interval', 0.0)),
        )
    if op == StepOpH2.EXPECT.value:
        return ExpectClientFrame(match=d.get('match') or {},
                                 timeout=d.get('timeout', 5.0))
    if op == StepOpH2.WAIT.value:
        return WaitForClientFrame(
            match=dict(d.get('match', {})),
            timeout=float(d.get('timeout', 5.0)),
        )
    if op == StepOpH2.SLEEP.value:
        return Sleep(duration=float(d['duration']))
    if op == StepOpH2.ABORT.value:
        return Abort()
    if op == StepOpH2.HALF_CLOSE.value:
        return HalfClose()
    if op == StepOpH2.GOAWAY_CLOSE.value:
        return CloseGracefully(
            error_code=int(d.get('error_code', 0)),
            last_stream_id=int(d.get('last_stream_id', 0)),
        )
    raise ValueError(f'unknown step op: {op!r}')


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

    Restricted to [`ROUND_TRIP_FRAME_CLASSES`][]; a frame
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
        base['append_data'] = base64.b64encode(
            getattr(frame, 'append_data', b'') or b'').decode('ascii')
    elif name == 'Ping':
        base['payload'] = base64.b64encode(
            getattr(frame, 'payload', b'') or b'').decode('ascii')
    elif name == 'Data':
        base['data'] = base64.b64encode(
            getattr(frame, 'payload', b'') or b'').decode('ascii')
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
    """Reconstruct a frame from [`_frame_to_dict`][]'s output."""
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
            + base64.b64decode(d.get('append_data', '')))
        return frame_types.GoAway(
            length=len(payload), type_=frame_types.FrameTypes.GOAWAY,
            flags=flags, stream_id=stream_id, data=payload)
    if name == 'Ping':
        # ``Ping`` alone among the frame classes built here requires
        # ``data``; omitting it makes a serialised PING unreadable back.
        payload = _payload_from_record(base64.b64decode(d.get('payload', '')))
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
        payload = _payload_from_record(base64.b64decode(d.get('data', '')))
        return frame_types.Data(
            length=len(payload), type_=frame_types.FrameTypes.DATA,
            flags=flags, stream_id=stream_id, data=payload)
    # Reachable only if a name is added to ROUND_TRIP_FRAME_CLASSES without a
    # reader arm here: the declaration and the codec disagree.
    raise ValueError(f'{name!r} is declared round-trippable but has no reader arm')


def scenario_to_json(scenario: ScenarioH2) -> str:
    """Serialise *scenario* to JSON Lines (one step per line).

    Header lines (``send_preface`` flag, ``initial_settings``) sit on
    the first line under the op ``HEADER`` so the file is one
    line-oriented stream with no out-of-band metadata.
    """
    lines = [json.dumps({
        'op': 'HEADER',
        'name': scenario.name,
        'send_preface': scenario.send_preface,
        'initial_settings': [list(p) for p in scenario.initial_settings],
    })]
    for step in scenario.steps:
        lines.append(json.dumps(_step_to_dict(step)))
    return '\n'.join(lines)


def scenario_from_json(src: str) -> ScenarioH2:
    """Parse JSON Lines back to a [`ScenarioH2`][]."""
    name = ''
    send_preface = True
    initial_settings: tuple[tuple[int, int], ...] = ()
    steps: list[H2Step] = []
    for line in src.splitlines():
        line = line.strip()
        if not line:
            continue
        d = json.loads(line)
        if d.get('op') == 'HEADER':
            name = d.get('name', '')
            send_preface = bool(d.get('send_preface', True))
            initial_settings = tuple(
                tuple(pair) for pair in d.get('initial_settings', []))
            continue
        steps.append(_step_from_dict(d))
    return ScenarioH2(
        steps=tuple(steps),
        send_preface=send_preface,
        initial_settings=initial_settings,
        name=name,
    )


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
    'scenario_from_json',
    'scenario_to_json',
]
