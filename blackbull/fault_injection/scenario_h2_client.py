"""Programmable HTTP/2 **client-side** scenario model.

A :class:`ScenarioH2Client` is a sequence of typed *steps* that
:meth:`blackbull.client.http2.HTTP2Client.execute_scenario` walks in order
against a live connection.  This is the client-side half of the HTTP/2
toolkit: a programmable client that drives a target *server* through
deliberate misbehaviour — a preface that never arrives, a header block
opened and abandoned, a Rapid Reset burst, a window never opened.

Its twin is :mod:`blackbull.fault_injection.scenario_h1`, the client-side
vocabulary one protocol over, and this module takes that twin's names
wherever the two mean the same thing: :class:`SendBytes`,
:class:`ReadResponse`, :class:`Sleep`, :class:`Abort`, and the fields of
:class:`ScenarioH2ClientResult`.  Sprint 107 learned why that matters the
expensive way — a vocabulary written from the *protocol* rather than from
its twin drifted three times in one sprint, and every drift was found by
someone asking rather than by reading the code.

Two steps have no HTTP/1.1 counterpart, and both earn it:

* :class:`SendPreface` — HTTP/1.1 has no connection preface.
* :class:`SendFrame` — HTTP/2 is framed where HTTP/1.1 is a byte stream, so
  the typed step builds a frame rather than a blob.

As on the server side, the bytes are assembled here rather than by the
production send path: a breaker that shares the production serialiser
cannot emit a fault that serialiser has.
"""
from __future__ import annotations

import enum
import json
from dataclasses import dataclass

#: RFC 9113 §3.4 — the client connection preface.
CLIENT_PREFACE = b'PRI * HTTP/2.0\r\n\r\nSM\r\n\r\n'


class StepOpH2Client(str, enum.Enum):
    """Tag used by the JSON serialiser."""
    PREFACE = 'PREFACE'
    SEND_FRAME = 'SEND_FRAME'
    SEND = 'SEND'
    SLEEP = 'SLEEP'
    READ = 'READ'
    ABORT = 'ABORT'


@dataclass(frozen=True)
class SendPreface:
    """Write the client connection preface (RFC 9113 §3.4).

    A step rather than a scenario flag, unlike ``ScenarioH2.send_preface``
    on the server side: a *client* scenario's whole point may be to delay
    the preface, split it, or never send it, and a boolean cannot say
    "after 30 seconds".
    """


@dataclass(frozen=True)
class SendFrame:
    """Emit one frame, built here rather than by the production sender.

    ``payload`` is the frame payload; the 9-byte header is assembled from
    the other fields.  Deliberately low-level: a fault scenario wants to
    set a length that disagrees with the payload, or a flag combination the
    typed frame classes refuse, and a step that went through
    ``FrameFactory`` could not.

    ``declared_length`` overrides the header's length field without
    changing the bytes actually written — the direct way to express "the
    peer lied about how much is coming".

    ``frame_type`` is the raw type byte: a :class:`~blackbull.protocol.frame_types.FrameTypes`
    member (which *is* a one-byte ``bytes``), or an ``int`` for a type the
    enum does not name — an unregistered type being itself a fault worth
    staging.
    """
    frame_type: bytes | int
    flags: int = 0
    stream_id: int = 0
    data: bytes = b''
    declared_length: int | None = None

    def __post_init__(self) -> None:
        # Normalise to the one-byte ``bytes`` a ``FrameTypes`` member already
        # is, so ``SendFrame(0xfa)`` and ``SendFrame(b'\xfa')`` are the same
        # step and a JSON round-trip compares equal to what it came from.
        ft = self.frame_type
        if not isinstance(ft, (bytes, bytearray)):
            object.__setattr__(self, 'frame_type', int(ft).to_bytes(1, 'big'))
        elif not isinstance(ft, bytes):
            object.__setattr__(self, 'frame_type', bytes(ft))


@dataclass(frozen=True)
class SendBytes:
    """Push arbitrary bytes at the server.

    The escape hatch, and the same name and fields the HTTP/1.1 client-side
    vocabulary uses.  ``byte_interval > 0`` transmits one byte at a time
    with that delay.
    """
    data: bytes
    byte_interval: float = 0.0


@dataclass(frozen=True)
class Sleep:
    """Idle for ``duration`` seconds without reading or writing."""
    duration: float


@dataclass(frozen=True)
class ReadResponse:
    """Read one frame from the server, or record a timeout."""
    timeout: float = 5.0


@dataclass(frozen=True)
class Abort:
    """Hard-close the connection (``transport.abort`` → RST on Linux).

    Terminal: later steps short-circuit, exactly as on the HTTP/1.1 side.
    """


Step = SendPreface | SendFrame | SendBytes | Sleep | ReadResponse | Abort


@dataclass(frozen=True)
class ScenarioH2Client:
    """An ordered sequence of steps, plus a name for parametrisation."""
    steps: tuple[Step, ...] = ()
    name: str = ''


@dataclass
class ScenarioH2ClientResult:
    """Outcome of one :meth:`HTTP2Client.execute_scenario` call.

    Field names come from
    :class:`~blackbull.fault_injection.scenario_h1.ScenarioResult`, its
    twin, so a harness reporting on one does not need a second spelling for
    the other.
    """
    #: The frame a ``ReadResponse`` step read, if any.
    response: object | None = None
    #: ``repr()`` of whatever went wrong, if anything.
    exception: str | None = None
    #: A ``ReadResponse`` step expired.
    timed_out: bool = False
    #: An ``Abort`` step ran.
    aborted: bool = False
    #: How many steps ran to completion.
    steps_completed: int = 0
    #: Seconds from the first step to the last.
    elapsed_s: float = 0.0


# ---------------------------------------------------------------------------
# JSON Lines serialisation — the shape both other scenario modules use
# ---------------------------------------------------------------------------


def _step_to_dict(step) -> dict:
    if isinstance(step, SendPreface):
        return {'op': StepOpH2Client.PREFACE}
    if isinstance(step, SendFrame):
        return {'op': StepOpH2Client.SEND_FRAME,
                'frame_type': step.frame_type[0],
                'flags': step.flags,
                'stream_id': step.stream_id,
                'data': step.data.hex(),
                'declared_length': step.declared_length}
    if isinstance(step, SendBytes):
        return {'op': StepOpH2Client.SEND, 'data': step.data.hex(),
                'byte_interval': step.byte_interval}
    if isinstance(step, Sleep):
        return {'op': StepOpH2Client.SLEEP, 'duration': step.duration}
    if isinstance(step, ReadResponse):
        return {'op': StepOpH2Client.READ, 'timeout': step.timeout}
    if isinstance(step, Abort):
        return {'op': StepOpH2Client.ABORT}
    raise ValueError(f'cannot serialise step: {step!r}')


def _step_from_dict(d: dict):
    op = d['op']
    if op == StepOpH2Client.PREFACE:
        return SendPreface()
    if op == StepOpH2Client.SEND_FRAME:
        # Restored as a one-byte ``bytes``, not an ``int``: ``FrameTypes``
        # *is* a bytes enum, so ``bytes([6]) == FrameTypes.PING`` while
        # ``6 == FrameTypes.PING`` is False — normalising to bytes is what
        # makes a round-trip compare equal to the scenario it came from.
        return SendFrame(frame_type=bytes([d['frame_type']]),
                         flags=d.get('flags', 0),
                         stream_id=d.get('stream_id', 0),
                         data=bytes.fromhex(d.get('data', '')),
                         declared_length=d.get('declared_length'))
    if op == StepOpH2Client.SEND:
        return SendBytes(data=bytes.fromhex(d['data']),
                         byte_interval=d.get('byte_interval', 0.0))
    if op == StepOpH2Client.SLEEP:
        return Sleep(duration=d['duration'])
    if op == StepOpH2Client.READ:
        return ReadResponse(timeout=d.get('timeout', 5.0))
    if op == StepOpH2Client.ABORT:
        return Abort()
    raise ValueError(f'unknown step op: {op!r}')


def scenario_to_json(scenario: ScenarioH2Client) -> str:
    """Serialise to JSON Lines, name on the first ``HEADER`` line."""
    lines = [json.dumps({'op': 'HEADER', 'name': scenario.name})]
    for step in scenario.steps:
        lines.append(json.dumps(_step_to_dict(step)))
    return '\n'.join(lines)


def scenario_from_json(src: str) -> ScenarioH2Client:
    """Parse JSON Lines back to a :class:`ScenarioH2Client`."""
    name = ''
    steps: list = []
    for line in src.splitlines():
        line = line.strip()
        if not line:
            continue
        d = json.loads(line)
        if d.get('op') == 'HEADER':
            name = d.get('name', '')
            continue
        steps.append(_step_from_dict(d))
    return ScenarioH2Client(steps=tuple(steps), name=name)


def encode_frame(step: SendFrame) -> bytes:
    """Assemble one frame's wire bytes from *step*.

    Here rather than in ``FrameBase.save()`` on purpose — the same rule the
    fault servers follow.  It is also what makes ``declared_length``
    possible: a header whose length disagrees with the payload is exactly
    the fault, and a serialiser that computed the length could not say it.
    """
    length = step.declared_length
    if length is None:
        length = len(step.data)
    return (
        length.to_bytes(3, 'big')
        + bytes(step.frame_type)
        + int(step.flags).to_bytes(1, 'big')
        + (int(step.stream_id) & 0x7fffffff).to_bytes(4, 'big')
        + step.data
    )
