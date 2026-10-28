"""HTTP/1.1 server-side scenarios for a target client.

Output is raw bytes, assembled here rather than by the production sender.
"""
from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import ClassVar

from . import _scenario
from ._scenario import Abort, HalfClose, SendRawBytes, Sleep


class StepOpH1Server(str, enum.Enum):
    """The v1 JSON tags, still read until v1 reading is removed."""
    WAIT_FOR_REQUEST = 'WAIT_FOR_REQUEST'
    EXPECT_REQUEST = 'EXPECT_REQUEST'
    SEND_RAW = 'SEND_RAW'
    SEND_STATUS_LINE = 'SEND_STATUS_LINE'
    SEND_HEADER = 'SEND_HEADER'
    END_HEADERS = 'END_HEADERS'
    SEND_CHUNK = 'SEND_CHUNK'
    END_CHUNKED_BODY = 'END_CHUNKED_BODY'
    SLEEP = 'SLEEP'
    ABORT = 'ABORT'
    CLOSE_GRACEFULLY = 'CLOSE_GRACEFULLY'
    HALF_CLOSE = 'HALF_CLOSE'


class WaitForRequest(_scenario._WaitFor):
    """Block until a request head arrives, optionally one that matches.

    On a pipelined connection (RFC 9112 §9.3.2) this is how a scenario
    breaks the POST and not the GET.  **Skipping desyncs the connection**,
    because HTTP/1.1 responses are positional: a head passed over is one the
    scenario can no longer answer.  [`ExpectRequest`][] skips nothing.
    """


class ExpectRequest(_scenario._Expect):
    """Read one request head and record whether it matched.

    A fault staged against ``Expect: 100-continue`` tests nothing if the
    client never sent that header; the verdict lands on
    ``ScenarioH1ServerResult.expectations``.
    """


@dataclass(frozen=True)
class SendStatusLine:
    """Emit status-line fields without validation; malformed values are intentional."""
    code: int = 200
    reason: str = 'OK'
    version: str = 'HTTP/1.1'
    #: Omit the reason phrase entirely (RFC 9112 §4 permits an empty one,
    #: which is not the same as omitting the space before it).
    omit_reason: bool = False

    def wire(self) -> bytes:
        head = f'{self.version} {self.code}'
        if not self.omit_reason:
            head += f' {self.reason}'
        return head.encode('latin-1') + b'\r\n'


@dataclass(frozen=True)
class SendHeader:
    """Emit a header; ``fold=True`` emits an obs-fold continuation (RFC 9112 §5.2)."""
    name: str
    value: str
    fold: bool = False

    def wire(self) -> bytes:
        if self.fold:
            return b' ' + self.value.encode('latin-1') + b'\r\n'
        return (self.name.encode('latin-1') + b': '
                + self.value.encode('latin-1') + b'\r\n')


@dataclass(frozen=True)
class EndHeaders:
    """The blank line that ends the head.  Omit it to stage a head that
    never finishes."""

    def wire(self) -> bytes:
        return b'\r\n'


@dataclass(frozen=True)
class SendChunk:
    """One chunk of a chunked body (RFC 9112 §7.1).

    ``declared_size`` sets the chunk-size line independently of the data —
    the single most common framing fault there is.  ``extension`` appends a
    chunk extension; ``terminator`` can be replaced to stage a bad CRLF.
    """
    data: bytes = b''
    declared_size: int | None = None
    extension: str = ''
    terminator: bytes = b'\r\n'

    def wire(self) -> bytes:
        size = len(self.data) if self.declared_size is None else self.declared_size
        line = format(size, 'x')
        if self.extension:
            line += f';{self.extension}'
        return line.encode('latin-1') + b'\r\n' + self.data + self.terminator


@dataclass(frozen=True)
class EndChunkedBody:
    """The zero-length chunk that terminates a chunked body, with any
    ``trailers``; omit this step to stage a body that never terminates."""
    trailers: tuple[tuple[str, str], ...] = ()

    def wire(self) -> bytes:
        return b'0\r\n' + b''.join(
            name.encode('latin-1') + b': ' + value.encode('latin-1') + b'\r\n'
            for name, value in self.trailers) + b'\r\n'


@dataclass(frozen=True)
class CloseGracefully:
    """Close cleanly (FIN) after whatever has been written.

    Terminal.  What the client sees is an orderly EOF, not the reset
    [`Abort`][] sends.
    """
    graceful: ClassVar[bool] = True

    def wire(self) -> bytes:
        return b''


Step = (WaitForRequest | ExpectRequest | SendStatusLine | SendHeader | EndHeaders
        | SendChunk | EndChunkedBody | SendRawBytes | Sleep | Abort
        | CloseGracefully | HalfClose)


@dataclass(frozen=True)
class ScenarioH1Server:
    """An ordered sequence of steps, plus a name for test parametrisation."""
    steps: tuple[Step, ...] = ()
    name: str = ''

    _v1: ClassVar = (False, {
        StepOpH1Server.WAIT_FOR_REQUEST: 'WaitForRequest',
        StepOpH1Server.EXPECT_REQUEST: 'ExpectRequest',
        StepOpH1Server.SEND_RAW: 'SendRawBytes', StepOpH1Server.SLEEP: 'Sleep',
        StepOpH1Server.ABORT: 'Abort',
        StepOpH1Server.CLOSE_GRACEFULLY: 'CloseGracefully',
        StepOpH1Server.HALF_CLOSE: 'HalfClose'})


# ---------------------------------------------------------------------------
# The match grammar
# ---------------------------------------------------------------------------


def parse_request_head(head: bytes) -> dict:
    """Split a request head into the fields [`request_matches`][] reads.

    Lenient by necessity: it parses what a *client under test* actually
    sent.  A malformed request line yields empty strings rather than
    raising, so a scenario matching on ``method`` simply will not match it.
    """
    lines = head.split(b'\r\n')
    parts = lines[0].split(b' ') if lines else []
    method, target, version = (
        (parts[i].decode('latin-1') if len(parts) > i else '') for i in range(3))
    headers: list[tuple[str, str]] = []
    for line in lines[1:]:
        if not line:
            break
        name, sep, value = line.partition(b':')
        if sep:
            headers.append((name.strip().decode('latin-1').lower(),
                            value.strip().decode('latin-1')))
    return {'method': method, 'target': target, 'version': version,
            'headers': headers}


def request_matches(head: bytes, match: dict) -> bool:
    """Return True iff *head* satisfies every key in *match*.

    Recognised keys: ``method``, ``target``, ``version``, ``header``,
    ``header_absent``.  **Unknown keys fail closed** — an unrecognised key
    is almost certainly a typo in a scenario, and silently matching on a
    key nobody reads would hide it.

    ``header`` takes ``(name, value)``; pass ``value=None`` to match on
    presence alone.  ``header_absent`` takes a name.
    """
    recognised = {'method', 'target', 'version', 'header', 'header_absent'}
    if set(match) - recognised:
        return False
    if not match:
        return True

    parsed = parse_request_head(head)
    for key in ('method', 'target', 'version'):
        if key in match and parsed[key] != match[key]:
            return False

    names = {n for n, _ in parsed['headers']}
    if 'header' in match:
        name, value = match['header']
        name = name.lower()
        if value is None:
            if name not in names:
                return False
        elif not any(n == name and v == value for n, v in parsed['headers']):
            return False
    if 'header_absent' in match:
        if match['header_absent'].lower() in names:
            return False
    return True


@dataclass
class ScenarioH1ServerResult:
    """What the executor observed while running a scenario.

    Field names match [`ScenarioH2Result`][blackbull.fault_injection.scenario_h2.ScenarioH2Result]
    wherever the two mean the same thing.
    """
    #: Steps that completed, in order.
    steps_completed: list = field(default_factory=list)
    #: Bytes the fault server wrote at the client.
    server_bytes_sent: int = 0
    #: Bytes the client sent us — the request head, mostly.
    client_bytes_received: int = 0
    #: Set when a step raised.
    exception: BaseException | None = None
    #: True when a ``WaitForRequest`` step expired without a request head.
    wait_timed_out: bool = False
    #: True when a terminal step (``Abort`` / ``CloseGracefully``) ran.
    terminated: bool = False
    #: Seconds from first connection to the last step.
    elapsed_s: float = 0.0
    #: The client's request head, once one arrived.
    request_head: bytes = b''
    #: Heads a ``WaitForRequest`` passed over; each desyncs the connection.
    wait_skipped: int = 0
    #: One ``(match, matched)`` pair per [`ExpectRequest`][] step, in order.
    expectations: list = field(default_factory=list)
    #: True on successful write-side half-close; False if absent or unsupported.
    half_closed: bool = False


def scenario_to_json(scenario: ScenarioH1Server) -> str:
    """Serialise *scenario* to JSON Lines (one step per line)."""
    return _scenario.to_json(scenario)


def scenario_from_json(src: str) -> ScenarioH1Server:
    """Parse JSON Lines back to a [`ScenarioH1Server`][]."""
    return _scenario.from_json(ScenarioH1Server, src)


def __getattr__(name):
    if name in ('encode_status_line', 'encode_header', 'encode_chunk',
                'encode_chunked_terminator'):
        return _scenario._deprecated_encoder(__name__, name)
    raise AttributeError(f'module {__name__!r} has no attribute {name!r}')
