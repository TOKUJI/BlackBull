"""Programmable HTTP/2 **client-side** scenario model.

A [`ScenarioH2Client`][] is a sequence of typed *steps* that
[`blackbull.client.http2.HTTP2Client.execute_scenario`][blackbull.client.http2.HTTP2Client.execute_scenario] walks in order
against a live connection.  This is the client-side half of the HTTP/2
toolkit: a programmable client that drives a target *server* through
deliberate misbehaviour — a preface that never arrives, a header block
opened and abandoned, a Rapid Reset burst, a window never opened.

Its twin is [`blackbull.fault_injection.scenario_h1`][blackbull.fault_injection.scenario_h1], the client-side
vocabulary one protocol over, and this module takes that twin's names
wherever the two mean the same thing: [`SendRawBytes`][],
[`ReadResponse`][], [`Sleep`][], [`Abort`][], and the fields of
[`ScenarioH2ClientResult`][].

Two steps have no HTTP/1.1 counterpart:

* [`SendPreface`][] — HTTP/1.1 has no connection preface.
* [`SendFrame`][] — HTTP/2 is framed where HTTP/1.1 is a byte stream, so
  the typed step builds a frame rather than a blob.

The bytes are assembled here and not by the production send path;
``docs/guide/fault_injection.md`` says why.
"""
from __future__ import annotations

import enum
import json

from blackbull.protocol.frame_types import FrameTypes
from dataclasses import dataclass, field

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
    HALF_CLOSE = 'HALF_CLOSE'
    WAIT_FOR_SERVER_FRAME = 'WAIT_FOR_SERVER_FRAME'
    EXPECT_SERVER_FRAME = 'EXPECT_SERVER_FRAME'


@dataclass(frozen=True)
class SendPreface:
    """Write the client connection preface (RFC 9113 §3.4).

    A step, not the boolean ``ScenarioH2.send_preface`` the server side
    has: delaying or splitting the preface is the fault, and a flag cannot
    say "after 30 seconds".
    """


@dataclass(frozen=True)
class SendFrame:
    """Emit one frame, built here rather than by the production sender.

    ``payload`` is the frame payload; the 9-byte header is assembled from
    the other fields.  Low-level on purpose: a length that disagrees with
    the payload, or a flag combination the typed frame classes refuse, is
    unreachable through ``FrameFactory``.

    ``declared_length`` overrides the header's length field without
    changing the bytes actually written — the direct way to express "the
    peer lied about how much is coming".

    ``frame_type`` is the raw type byte: a [`FrameTypes`][blackbull.protocol.frame_types.FrameTypes]
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
class SendHeaders:
    """Emit a HEADERS frame, with the header block built for you.

    ``pseudo`` and ``headers`` are encoded with HPACK in the order given, so
    a scenario can put ``:path`` after a regular field (RFC 9113 §8.3
    forbids it) simply by saying so.  Nothing here validates: the whole
    point is to send what a conforming client would not.

    ``raw_block`` replaces the encoded block outright, for faults HPACK
    itself cannot produce — a truncated block, an invalid table index, a
    Huffman string that does not decode.  When set, ``pseudo`` and
    ``headers`` are ignored.
    """
    pseudo: tuple[tuple[str, str], ...] = ()
    headers: tuple[tuple[str, str], ...] = ()
    stream_id: int = 1
    end_stream: bool = False
    end_headers: bool = True
    raw_block: bytes | None = None
    declared_length: int | None = None


@dataclass(frozen=True)
class SendRawBytes:
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


@dataclass(frozen=True)
class WaitForServerFrame:
    """Read frames until one satisfies ``match``, or the timeout wins.

    A **filter**: non-matching frames are read, counted in
    ``wait_skipped``, and passed over.  Twin of
    [`WaitForClientFrame`][blackbull.fault_injection.scenario_h2.WaitForClientFrame].

    This is what makes an HTTP/2 client scenario able to observe a
    *verdict*.  A single ``ReadResponse`` cannot: the first frame any
    correct server sends is its handshake SETTINGS, so a GOAWAY or
    RST_STREAM is always further down the stream, at a depth that varies
    by peer.  A scenario that had to guess that depth was a scenario
    written against one server.
    """
    match: dict = field(default_factory=dict)
    timeout: float = 5.0


@dataclass(frozen=True)
class ExpectServerFrame:
    """Read one frame and record whether it matched.

    A **guard**, not a filter: nothing is skipped and the executor moves
    on either way.  Twin of
    [`ExpectClientFrame`][blackbull.fault_injection.scenario_h2.ExpectClientFrame].
    """
    match: dict = field(default_factory=dict)
    timeout: float = 5.0


@dataclass(frozen=True)
class HalfClose:
    """Shut down the sending direction only (FIN), keep reading.

    **Not terminal** — later steps still run, which is the whole point.
    ``Abort`` is not a substitute: it sends RST, discarding what is buffered
    and leaving nothing to read.
    """


Step = (SendPreface | SendFrame | SendHeaders | SendRawBytes | Sleep
        | ReadResponse | Abort | HalfClose | WaitForServerFrame
        | ExpectServerFrame)


@dataclass(frozen=True)
class ScenarioH2Client:
    """An ordered sequence of steps, plus a name for parametrisation."""
    steps: tuple[Step, ...] = ()
    name: str = ''


@dataclass
class ScenarioH2ClientResult:
    """Outcome of one ``HTTP2Client.execute_scenario`` call.

    Field names come from
    [`ScenarioResult`][blackbull.fault_injection.scenario_h1.ScenarioResult], its
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
    #: True when a ``HalfClose`` step actually shut down the write side.
    #: False both when no such step ran and when the transport refused it
    #: (TLS has no half-close), so a test can tell "did not ask" from
    #: "asked and it did not happen" — a silently skipped half-close
    #: otherwise reads as a pass.
    half_closed: bool = False
    #: Everything a read step received, in order.  ``response`` stays the
    #: most recent one for back-compat; this is what a scenario needs when
    #: the peer sends more than one thing — a pipelined pair on HTTP/1.1, or
    #: the handshake frames an HTTP/2 verdict arrives behind.  Before it
    #: existed, the second read overwrote the first and the loss was silent.
    received: list = field(default_factory=list)
    #: Bytes read from the peer.  Named for who the peer is, mirroring
    #: ``client_bytes_received`` on the broken-server results.
    server_bytes_received: int = 0
    #: One ``(match, matched)`` pair per guard step, in order: what the
    #: scenario assumed, and whether it held.  Same shape and same name as
    #: the broken-server half.
    expectations: list = field(default_factory=list)
    #: Messages a ``WaitFor…(match=...)`` step read and passed over.
    wait_skipped: int = 0
    #: Whether a ``WaitFor…`` step timed out before its match arrived.  The
    #: step still counts as completed and the next step runs; this is what
    #: distinguishes a per-step miss from a transport error in ``exception``.
    wait_timed_out: bool = False


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
    if isinstance(step, SendRawBytes):
        return {'op': StepOpH2Client.SEND, 'data': step.data.hex(),
                'byte_interval': step.byte_interval}
    if isinstance(step, Sleep):
        return {'op': StepOpH2Client.SLEEP, 'duration': step.duration}
    if isinstance(step, ReadResponse):
        return {'op': StepOpH2Client.READ, 'timeout': step.timeout}
    if isinstance(step, Abort):
        return {'op': StepOpH2Client.ABORT}
    if isinstance(step, HalfClose):
        return {'op': StepOpH2Client.HALF_CLOSE}
    if isinstance(step, WaitForServerFrame):
        return {'op': StepOpH2Client.WAIT_FOR_SERVER_FRAME,
                'timeout': step.timeout, 'match': dict(step.match)}
    if isinstance(step, ExpectServerFrame):
        return {'op': StepOpH2Client.EXPECT_SERVER_FRAME,
                'timeout': step.timeout, 'match': dict(step.match)}
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
        return SendRawBytes(data=bytes.fromhex(d['data']),
                         byte_interval=d.get('byte_interval', 0.0))
    if op == StepOpH2Client.SLEEP:
        return Sleep(duration=d['duration'])
    if op == StepOpH2Client.READ:
        return ReadResponse(timeout=d.get('timeout', 5.0))
    if op == StepOpH2Client.ABORT:
        return Abort()
    if op == StepOpH2Client.HALF_CLOSE:
        return HalfClose()
    if op == StepOpH2Client.WAIT_FOR_SERVER_FRAME:
        return WaitForServerFrame(timeout=d.get('timeout', 5.0),
                                  match=dict(d.get('match') or {}))
    if op == StepOpH2Client.EXPECT_SERVER_FRAME:
        return ExpectServerFrame(timeout=d.get('timeout', 5.0),
                                 match=dict(d.get('match') or {}))
    raise ValueError(f'unknown step op: {op!r}')


def scenario_to_json(scenario: ScenarioH2Client) -> str:
    """Serialise to JSON Lines, name on the first ``HEADER`` line."""
    lines = [json.dumps({'op': 'HEADER', 'name': scenario.name})]
    for step in scenario.steps:
        lines.append(json.dumps(_step_to_dict(step)))
    return '\n'.join(lines)


def scenario_from_json(src: str) -> ScenarioH2Client:
    """Parse JSON Lines back to a [`ScenarioH2Client`][]."""
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


def encode_headers(step: SendHeaders) -> bytes:
    """Assemble one HEADERS frame from *step*.

    HPACK encoding goes through the ``hpack`` package the server also uses,
    because a *correct* block is the baseline every header fault is a
    deviation from — hand-rolling it would make even the well-formed case a
    guess.  ``raw_block`` is the escape hatch for blocks HPACK will not
    produce.
    """
    if step.raw_block is not None:
        block = step.raw_block
    else:
        from hpack import Encoder  # noqa: PLC0415
        block = Encoder().encode(list(step.pseudo) + list(step.headers))
    flags = 0
    if step.end_stream:
        flags |= 0x01
    if step.end_headers:
        flags |= 0x04
    return encode_frame(SendFrame(
        frame_type=FrameTypes.HEADERS, flags=flags, stream_id=step.stream_id,
        data=block, declared_length=step.declared_length))


def encode_frame(step: SendFrame) -> bytes:
    """Assemble one frame's wire bytes from *step*.

    Here and not in ``FrameBase.save()``, which is what makes
    ``declared_length`` possible: a serialiser that computed the length
    could not state one that disagrees with the payload.
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

# ``SendRawBytes`` is the canonical spelling in all four scenario
# vocabularies; ``SendBytes`` is the deprecated client-side one.

def __getattr__(name: str):
    """PEP 562 — warn when the deprecated spelling is actually used.

    A module-level assignment would alias silently; going through
    ``__getattr__`` means only a caller who reaches for ``SendBytes`` is
    warned, and is warned at their own call site.
    """
    if name == 'SendBytes':
        import warnings  # noqa: PLC0415
        warnings.warn(
            f"{__name__}.SendBytes is deprecated; use SendRawBytes, the "
            "name the other three scenario vocabularies use.  Removal no "
            "earlier than 2027-08-19.",
            DeprecationWarning, stacklevel=2)
        return SendRawBytes
    raise AttributeError(f'module {__name__!r} has no attribute {name!r}')
