"""Programmable HTTP/2 wire-level scenario model.

A :class:`ScenarioH2` is a sequence of typed *steps* that the
:class:`blackbull.fault_injection.h2_server.H2FaultServer` executor
walks in order against a connected HTTP/2 client.  This is the
*server-side* half of the :mod:`blackbull.fault_injection` toolkit:
a programmable server that emits deliberate misbehaviour toward a
client — half-closed streams, exhausted flow-control windows,
illegal SETTINGS, weird frame sequences — expressed as data, not
procedural test code.

The symmetric *client-side* half (programmable HTTP/1.1 client
driving deliberate misbehaviour toward a server) lives in
:mod:`blackbull.fault_injection.scenario_h1`.

Use cases:

  * HTTP/2 client-library authors testing their client's resilience
    against a misbehaving server.
  * Proxy / load-balancer authors testing what their transit code
    does when an upstream emits illegal frame sequences.
  * Security researchers reproducing CVE-class patterns
    (CONTINUATION-flood, RST-flood) from a deterministic harness.

Steps
-----

* :class:`SendFrame` — emit one parsed :class:`FrameBase` instance.
  The executor handles serialisation through the existing
  :class:`~blackbull.protocol.frame.FrameFactory`.
* :class:`SendRawBytes` — escape hatch for bytes the framework's
  ``FrameFactory`` cannot construct (e.g. illegal frame types,
  oversized frames, malformed length fields).
* :class:`WaitForClientFrame` — pause until an inbound frame from
  the client matches the *declarative match dict*.  Fields supported:

  ===============  =========================================
  ``type``         ``'HEADERS'``, ``'SETTINGS'``, etc.
  ``stream_id``    Integer; ``None`` = any.
  ``flags_set``    List of flag names (uppercase) that must be set.
  ``flags_unset``  List of flag names that must be unset.
  ===============  =========================================

* :class:`Sleep` — idle without sending or reading.
* :class:`Abort` — hard-close the underlying transport (RST on
  Linux).
* :class:`CloseGracefully` — send a GOAWAY frame, then close
  cleanly.

Serialisation
-------------

:meth:`ScenarioH2.to_json` / :meth:`ScenarioH2.from_json` round-trip
through JSON Lines.  Because :class:`SendFrame` carries a frame
object, round-tripping requires the frame to be reconstructable from
its serialised form — currently SETTINGS, WINDOW_UPDATE, RST_STREAM,
GOAWAY, PING, and a typed-payload form of HEADERS / DATA.  For ad-hoc
frames the caller passes through :class:`SendRawBytes`, which is
always round-trippable.
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
    SLEEP = 'SLEEP'
    ABORT = 'ABORT'
    GOAWAY_CLOSE = 'GOAWAY_CLOSE'


@dataclass(frozen=True)
class SendFrame:
    """Emit one parsed frame onto the connection.

    Routed through :class:`~blackbull.protocol.frame.FrameFactory` so
    the on-wire serialisation matches the framework's normal output.
    Use :class:`SendRawBytes` for frames the factory cannot construct.
    """
    frame: Frame


@dataclass(frozen=True)
class SendRawBytes:
    """Push arbitrary bytes onto the connection.

    Escape hatch for malformed frames (illegal type byte, length
    exceeding ``SETTINGS_MAX_FRAME_SIZE``, etc.) that the typed
    :class:`SendFrame` path will not produce.

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
    :class:`ScenarioH2Result` and proceeds to the next step.
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
    just like :class:`Abort`).  ``error_code`` is one of the
    :class:`~blackbull.protocol.frame_types.ErrorCodes` values;
    ``last_stream_id`` advertises the last stream the server is
    willing to process — pass ``0`` to refuse all client streams,
    or the highest accepted stream ID otherwise.
    """
    error_code: int = 0
    last_stream_id: int = 0


# Discriminated union the H2 executor matches on.
H2Step = Union[
    SendFrame,
    SendRawBytes,
    WaitForClientFrame,
    Sleep,
    Abort,
    CloseGracefully,
]


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


@dataclass
class ScenarioH2Result:
    """Outcome of one :class:`ScenarioH2` run.

    Mirrors :class:`~blackbull.fault_injection.scenario_h1.ScenarioResult`'s
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

    # Whether a WaitForClientFrame step timed out before its match
    # arrived.  When True the step still counts as completed and the
    # next step runs; this distinguishes a per-step timeout (recorded
    # here) from a transport-level error (recorded in ``exception``).
    wait_timed_out: bool = False

    # True when execution stopped because an Abort or CloseGracefully
    # step ran.
    terminated: bool = False

    elapsed_s: float = 0.0


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
    ``flags_unset``.  Unknown keys fail closed — an unrecognised
    match key is almost certainly a typo in a catalogue entry, and
    silently matching on a missing key would hide the bug.
    """
    recognised = {'type', 'stream_id', 'flags_set', 'flags_unset'}
    extra = set(match) - recognised
    if extra:
        return False

    if 'type' in match:
        expected = match['type']
        if type(frame).__name__.upper() != expected.upper() \
                and _frame_type_name(frame) != expected.upper():
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
        }
    if isinstance(step, SendRawBytes):
        return {
            'op': StepOpH2.SEND_RAW.value,
            'data': base64.b64encode(step.data).decode('ascii'),
            'byte_interval': step.byte_interval,
        }
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
        return SendFrame(frame=_frame_from_dict(d['frame']))
    if op == StepOpH2.SEND_RAW.value:
        return SendRawBytes(
            data=base64.b64decode(d['data']),
            byte_interval=float(d.get('byte_interval', 0.0)),
        )
    if op == StepOpH2.WAIT.value:
        return WaitForClientFrame(
            match=dict(d.get('match', {})),
            timeout=float(d.get('timeout', 5.0)),
        )
    if op == StepOpH2.SLEEP.value:
        return Sleep(duration=float(d['duration']))
    if op == StepOpH2.ABORT.value:
        return Abort()
    if op == StepOpH2.GOAWAY_CLOSE.value:
        return CloseGracefully(
            error_code=int(d.get('error_code', 0)),
            last_stream_id=int(d.get('last_stream_id', 0)),
        )
    raise ValueError(f'unknown step op: {op!r}')


def _frame_to_dict(frame: Frame) -> dict:
    """Serialise a parsed frame to a round-trippable dict.

    Restricted to the frame types catalogue scenarios actually emit:
    SETTINGS, WINDOW_UPDATE, RST_STREAM, GOAWAY, PING, plus a
    typed-payload form of DATA (headers stay opt-out — sending real
    HEADERS frames usually wants the executor's HPACK encoder, not
    a serialised one).  For anything else, the caller should pass
    through :class:`SendRawBytes`, which is always round-trippable.
    """
    name = type(frame).__name__
    base = {
        'class': name,
        'stream_id': getattr(frame, 'stream_id', 0),
        'flags': int(getattr(frame, 'flags', 0) or 0),
    }
    if name == 'SettingFrame':
        base['settings'] = list(getattr(frame, 'settings', []) or [])
    elif name == 'WindowUpdate':
        base['window_size_increment'] = getattr(
            frame, 'window_size_increment', 0)
    elif name == 'RstStream':
        base['error_code'] = int(getattr(frame, 'error_code', 0))
    elif name == 'GoAway':
        base['last_stream_id'] = int(getattr(frame, 'last_stream_id', 0))
        base['error_code'] = int(getattr(frame, 'error_code', 0))
    elif name == 'Ping':
        base['payload'] = base64.b64encode(
            getattr(frame, 'payload', b'') or b'').decode('ascii')
    elif name == 'Data':
        base['data'] = base64.b64encode(
            getattr(frame, 'data', b'') or b'').decode('ascii')
    else:
        raise TypeError(
            f'{name} cannot be round-tripped through scenario_h2 JSON; '
            f'use SendRawBytes instead.')
    return base


def _frame_from_dict(d: dict) -> Frame:
    """Reconstruct a frame from :func:`_frame_to_dict`'s output."""
    from blackbull.protocol import frame_types  # local; avoids import-time cost

    name = d['class']
    stream_id = int(d.get('stream_id', 0))
    flags = int(d.get('flags', 0))
    if name == 'SettingFrame':
        f = frame_types.SettingFrame(length=0, type_=frame_types.FrameTypes.SETTINGS,
                                     flags=flags, stream_id=stream_id)
        f.settings = [tuple(pair) for pair in d.get('settings', [])]
        return f
    if name == 'WindowUpdate':
        f = frame_types.WindowUpdate(length=0, type_=frame_types.FrameTypes.WINDOW_UPDATE,
                                     flags=flags, stream_id=stream_id)
        f.window_size_increment = int(d.get('window_size_increment', 0))
        return f
    if name == 'RstStream':
        f = frame_types.RstStream(length=0, type_=frame_types.FrameTypes.RST_STREAM,
                                  flags=flags, stream_id=stream_id)
        f.error_code = int(d.get('error_code', 0))
        return f
    if name == 'GoAway':
        f = frame_types.GoAway(length=0, type_=frame_types.FrameTypes.GOAWAY,
                               flags=flags, stream_id=stream_id)
        f.last_stream_id = int(d.get('last_stream_id', 0))
        f.error_code = int(d.get('error_code', 0))
        return f
    if name == 'Ping':
        f = frame_types.Ping(length=0, type_=frame_types.FrameTypes.PING,
                             flags=flags, stream_id=stream_id)
        f.payload = base64.b64decode(d.get('payload', ''))
        return f
    if name == 'Data':
        f = frame_types.Data(length=0, type_=frame_types.FrameTypes.DATA,
                             flags=flags, stream_id=stream_id)
        f.data = base64.b64decode(d.get('data', ''))
        return f
    raise ValueError(f'unknown frame class: {name!r}')


def scenario_to_json(scenario: ScenarioH2) -> str:
    """Serialise *scenario* to JSON Lines (one step per line).

    Header lines (``send_preface`` flag, ``initial_settings``) sit on
    the first line under the op ``HEADER`` so the file is one
    line-oriented stream with no out-of-band metadata.
    """
    lines = [json.dumps({
        'op': 'HEADER',
        'send_preface': scenario.send_preface,
        'initial_settings': [list(p) for p in scenario.initial_settings],
    })]
    for step in scenario.steps:
        lines.append(json.dumps(_step_to_dict(step)))
    return '\n'.join(lines)


def scenario_from_json(src: str) -> ScenarioH2:
    """Parse JSON Lines back to a :class:`ScenarioH2`."""
    send_preface = True
    initial_settings: tuple[tuple[int, int], ...] = ()
    steps: list[H2Step] = []
    for line in src.splitlines():
        line = line.strip()
        if not line:
            continue
        d = json.loads(line)
        if d.get('op') == 'HEADER':
            send_preface = bool(d.get('send_preface', True))
            initial_settings = tuple(
                tuple(pair) for pair in d.get('initial_settings', []))
            continue
        steps.append(_step_from_dict(d))
    return ScenarioH2(
        steps=tuple(steps),
        send_preface=send_preface,
        initial_settings=initial_settings,
    )


__all__ = [
    'Abort',
    'CloseGracefully',
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
