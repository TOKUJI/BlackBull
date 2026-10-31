"""HTTP/2 client-side scenarios for a target server.

Assemble fault bytes independently of production request sending. Shared
transport steps do not make the two wire roles interchangeable.
"""
from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import ClassVar

from blackbull.protocol.frame_types import FrameTypes

from . import _scenario
from ._scenario import Abort, HalfClose, ReadResponse, SendRawBytes, Sleep
from .scenario_h2 import frame_bytes

#: RFC 9113 §3.4 — the client connection preface.
CLIENT_PREFACE = b'PRI * HTTP/2.0\r\n\r\nSM\r\n\r\n'


class StepOpH2Client(str, enum.Enum):
    """The v1 JSON tags, still read until v1 reading is removed."""
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

    def wire(self) -> bytes:
        return CLIENT_PREFACE


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

    def wire(self) -> bytes:
        length = len(self.data) if self.declared_length is None else self.declared_length
        return frame_bytes(length, self.frame_type, self.flags, self.stream_id, self.data)


@dataclass(frozen=True)
class SendHeaders:
    """Emit a HEADERS frame, with the header block built for you.

    ``pseudo`` and ``headers`` are encoded with HPACK in the order given, so
    a scenario can put ``:path`` after a regular field (RFC 9113 §8.3
    forbids it) simply by saying so.  Nothing here validates: the whole
    point is to send what a conforming client would not.  The block uses
    only the static table and never indexes, so it decodes the same on any
    connection and leaves the peer's dynamic table untouched.

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

    def wire(self) -> bytes:
        if self.raw_block is not None:
            block = self.raw_block
        else:
            from hpack import Encoder, NeverIndexedHeaderTuple  # noqa: PLC0415
            block = Encoder().encode([NeverIndexedHeaderTuple(n, v)
                                      for n, v in self.pseudo + self.headers])
        flags = (0x01 if self.end_stream else 0) | (0x04 if self.end_headers else 0)
        length = len(block) if self.declared_length is None else self.declared_length
        return frame_bytes(length, FrameTypes.HEADERS.value, flags, self.stream_id, block)


class WaitForServerFrame(_scenario._WaitFor):
    """Filter frames until one matches — how a verdict behind the server's
    handshake SETTINGS is reached."""


class ExpectServerFrame(_scenario._Expect):
    """Read one frame and record whether it matched."""


Step = (SendPreface | SendFrame | SendHeaders | SendRawBytes | Sleep
        | ReadResponse | Abort | HalfClose | WaitForServerFrame
        | ExpectServerFrame)


@dataclass(frozen=True)
class ScenarioH2Client:
    """An ordered sequence of steps, plus a name for parametrisation."""
    steps: tuple[Step, ...] = ()
    name: str = ''

    _v1: ClassVar = (False, {
        StepOpH2Client.PREFACE: 'SendPreface',
        StepOpH2Client.SEND_FRAME: 'SendFrame', StepOpH2Client.SEND: 'SendRawBytes',
        StepOpH2Client.SLEEP: 'Sleep', StepOpH2Client.READ: 'ReadResponse',
        StepOpH2Client.ABORT: 'Abort', StepOpH2Client.HALF_CLOSE: 'HalfClose',
        StepOpH2Client.WAIT_FOR_SERVER_FRAME: 'WaitForServerFrame',
        StepOpH2Client.EXPECT_SERVER_FRAME: 'ExpectServerFrame'})


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
    #: ``repr()`` of a peer or transport failure other than a close; the
    #: harness's own exceptions propagate.
    exception: str | None = None
    #: A ``ReadResponse`` step expired.
    timed_out: bool = False
    #: An ``Abort`` step ran.
    aborted: bool = False
    #: How many steps ran to completion.
    steps_completed: int = 0
    #: Seconds from the first step to the last.
    elapsed_s: float = 0.0
    #: True on successful write-side half-close; False if absent or unsupported.
    half_closed: bool = False
    # All responses in wire order; response remains the most recent one.
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
    #: The peer finished sending (later steps still run) or reset the connection.
    peer_closed: bool = False


def scenario_to_json(scenario: ScenarioH2Client) -> str:
    """Serialise to JSON Lines, name on the first ``HEADER`` line."""
    return _scenario.to_json(scenario)


def scenario_from_json(src: str) -> ScenarioH2Client:
    """Parse JSON Lines back to a [`ScenarioH2Client`][]."""
    return _scenario.from_json(ScenarioH2Client, src)


# ``SendRawBytes`` is the canonical spelling in all four scenario
# vocabularies; ``SendBytes`` is the deprecated client-side one.

# Unannotated: see tests/unit/test_deprecated_send_bytes_spellings.py::test_the_warning_is_attributed_to_the_callers_line.
def __getattr__(name):
    """Deprecated SendBytes alias for SendRawBytes (BBD005).
    """
    if name == 'SendBytes':
        from ..deprecations import _warn_deprecated  # noqa: PLC0415
        _warn_deprecated('BBD005', f'{__name__}.SendBytes',
                        'SendRawBytes, the name the other three scenario vocabularies use')
        return SendRawBytes
    if name in ('encode_frame', 'encode_headers'):
        return _scenario._deprecated_encoder(__name__, name)
    raise AttributeError(f'module {__name__!r} has no attribute {name!r}')
