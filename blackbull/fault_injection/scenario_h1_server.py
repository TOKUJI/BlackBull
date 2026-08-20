"""Programmable HTTP/1.1 **server-side** scenario model.

A :class:`ScenarioH1Server` is a sequence of typed *steps* that
:class:`~blackbull.fault_injection.h1_server.H1FaultServer` walks in order
against a connected client.  This is the server-side half of the HTTP/1.1
toolkit: a programmable server that drives a target *client* through
deliberate misbehaviour — a status line delivered a byte at a time, a
``Content-Length`` that overstates the body, a chunked body that stops
mid-chunk, a connection dropped mid-response.

The symmetric client-side half — a programmable *client* driving a real
server — is :mod:`blackbull.fault_injection.scenario_h1`.  Its vocabulary
looks similar and is **not** reusable here: ``ReadResponse`` and
``SendBytes`` name the other end of the wire.  Two vocabularies, because
there are two roles.

Everything a scenario emits is **raw bytes**, deliberately.  There is no
typed ``SendResponse`` step, because a response object would be built by
the production response path, and a fault server that shares the
production serialiser cannot produce a fault that serialiser has.  The
HTTP/2 half made the same choice for the same reason (it carries its own
frame encoder rather than calling ``FrameBase.save()``).
"""
from __future__ import annotations

import enum
import json
from dataclasses import dataclass, field


class StepOpH1Server(str, enum.Enum):
    """Tag used by the JSON serialiser."""
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


@dataclass(frozen=True)
class WaitForRequest:
    """Block until a request head arrives, optionally one that matches.

    A scenario that writes before the request is read is testing a
    different thing — an unsolicited response — and can simply omit this
    step.  On ``timeout`` expiry the executor records the miss and
    proceeds, matching ``WaitForClientFrame`` on the HTTP/2 side.

    With ``match`` set, heads that do not match are **read and skipped**,
    and the step keeps waiting — the same filter-over-a-stream meaning
    ``WaitForClientFrame`` has.  On HTTP/1.1 that stream is a pipeline
    (RFC 9112 §9.3.2), so this is how a scenario misbehaves at one request
    among several: answer the GET normally, break on the POST.

    **Skipping desyncs the connection, and that is not hidden.**  HTTP/1.1
    responses are positional — a skipped request is one the scenario can
    no longer answer, so everything after it is off by one.  On HTTP/2 the
    equivalent is harmless because streams are independent; here it is a
    fault in its own right, staged deliberately or not at all.  The count
    lands on ``ScenarioH1ServerResult.requests_skipped`` so a scenario
    author reads it from the result rather than deducing it.

    Use :class:`ExpectRequest` when the question is "did the client send
    what this scenario assumes" — that one reads a single head and skips
    nothing.
    """
    match: dict = field(default_factory=dict)
    timeout: float = 5.0


@dataclass(frozen=True)
class ExpectRequest:
    """Read one request head and record whether it matched.

    A guard, not a filter: nothing is skipped and the connection stays in
    step.  It answers a different question from :class:`WaitForRequest` —
    *is the client under test behaving as this scenario assumes?*  A
    scenario that stages a fault against `Expect: 100-continue` is testing
    nothing at all if the client never sent that header, and without this
    the run would look like a pass.

    A mismatch is **recorded, not raised**:
    ``ScenarioH1ServerResult.expectations`` collects one
    ``(match, matched)`` pair per step, so a scenario reports what it
    assumed alongside what it got.

    Deliberately not called ``WaitForRequest(match=...)`` even though the
    grammar is the same: reusing a name for a different meaning is the
    thing the 107+108 consistency sweep was run to prevent.
    """
    match: dict = field(default_factory=dict)
    timeout: float = 5.0


@dataclass(frozen=True)
class SendRawBytes:
    """Push arbitrary bytes at the client.

    The only way this server emits anything.  ``byte_interval > 0``
    transmits one byte at a time with that delay, which is how a trickled
    status line or a slow header block is expressed — the bytes are all
    legal and the *pacing* is the fault, so it cannot be spelled any other
    way.
    """
    data: bytes
    byte_interval: float = 0.0


@dataclass(frozen=True)
class SendStatusLine:
    """Emit a status line, field by field.

    Added by the 107+108 consistency sweep.  Nothing validates: a status
    line with no reason phrase, an impossible version, or a three-digit
    code that is not a status are all faults worth staging, and a typed
    step that refused them would be useless here.  What it buys over raw
    bytes is that the *shape* is legible — a reader sees which field the
    scenario is bending.
    """
    code: int = 200
    reason: str = 'OK'
    version: str = 'HTTP/1.1'
    #: Omit the reason phrase entirely (RFC 9112 §4 permits an empty one,
    #: which is not the same as omitting the space before it).
    omit_reason: bool = False


@dataclass(frozen=True)
class SendHeader:
    """Emit one header line.

    ``fold`` writes it as an obs-fold continuation (RFC 9112 §5.2, which
    deprecates the form and requires a recipient to reject or normalise
    it) — expressible before only as a hand-built byte string.
    """
    name: str
    value: str
    fold: bool = False


@dataclass(frozen=True)
class EndHeaders:
    """The blank line that ends the head.  Omit it to stage a head that
    never finishes."""


@dataclass(frozen=True)
class SendChunk:
    """One chunk of a chunked body (RFC 9112 §7.1).

    ``declared_size`` sets the chunk-size line independently of the data —
    the HTTP/1.1 twin of ``SendFrame.declared_length``, and the single most
    common framing fault there is.  ``extension`` appends a chunk
    extension; ``terminator`` can be replaced to stage a bad CRLF.
    """
    data: bytes = b''
    declared_size: int | None = None
    extension: str = ''
    terminator: bytes = b'\r\n'


@dataclass(frozen=True)
class EndChunkedBody:
    """The zero-length chunk that terminates a chunked body.

    ``trailers`` are emitted before the final CRLF; omit this step to stage
    a body that never terminates.
    """
    trailers: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class Sleep:
    """Idle for ``duration`` seconds, holding the connection open.

    Distinct from a slow send: nothing is written at all, so this is what
    a client's own response deadline is measured against.
    """
    duration: float


@dataclass(frozen=True)
class Abort:
    """Hard-close the connection (``transport.abort`` → RST on Linux).

    Terminal: later steps short-circuit.
    """


@dataclass(frozen=True)
class CloseGracefully:
    """Close cleanly (FIN) after whatever has been written.

    Terminal.  The difference from :class:`Abort` is what the client sees
    — an orderly EOF mid-body rather than a reset — and clients do not
    always treat the two alike, which is the point of having both.
    """


@dataclass(frozen=True)
class HalfClose:
    """Shut down the sending direction only (FIN), keep reading.

    Neither :class:`Abort` nor a full close says this.  ``Abort`` sends RST,
    which discards whatever is buffered and leaves nothing to read; a full
    close ends both directions at once.  A half-close is the ordinary end of
    a non-keep-alive exchange — "I have finished sending, I am still waiting
    for your answer" — and it is a distinct code path on the peer.

    **Not terminal**: later steps still run, because continuing to read is
    the whole point.
    """


Step = (WaitForRequest | ExpectRequest | SendStatusLine | SendHeader | EndHeaders
        | SendChunk | EndChunkedBody | SendRawBytes | Sleep | Abort
        | CloseGracefully | HalfClose)


@dataclass(frozen=True)
class ScenarioH1Server:
    """An ordered sequence of steps, plus a name for test parametrisation.

    ``steps`` is a **tuple**, matching :class:`ScenarioH2` — a frozen
    dataclass holding a mutable list is a frozen container of mutable
    contents, and the HTTP/2 half settled the question first.
    """
    steps: tuple[Step, ...] = ()
    name: str = ''


# ---------------------------------------------------------------------------
# The match grammar
# ---------------------------------------------------------------------------


def parse_request_head(head: bytes) -> dict:
    """Split a request head into the fields :func:`request_matches` reads.

    Deliberately lenient: this parses what a *client under test* actually
    sent, including things a conforming parser would reject, because a
    scenario may well be waiting for exactly that.  A malformed request
    line yields empty strings rather than raising — a scenario matching on
    ``method`` simply will not match it.
    """
    lines = head.split(b'\r\n')
    request_line = lines[0] if lines else b''
    parts = request_line.split(b' ')
    method = parts[0].decode('latin-1') if len(parts) > 0 else ''
    target = parts[1].decode('latin-1') if len(parts) > 1 else ''
    version = parts[2].decode('latin-1') if len(parts) > 2 else ''
    headers: list[tuple[str, str]] = []
    for line in lines[1:]:
        if not line:
            break
        name, sep, value = line.partition(b':')
        if not sep:
            continue
        headers.append((name.strip().decode('latin-1').lower(),
                        value.strip().decode('latin-1')))
    return {'method': method, 'target': target, 'version': version,
            'headers': headers}


def request_matches(head: bytes, match: dict) -> bool:
    """Return True iff *head* satisfies every key in *match*.

    Recognised keys: ``method``, ``target``, ``version``, ``header``,
    ``header_absent``.  **Unknown keys fail closed** — an unrecognised key
    is almost certainly a typo in a scenario, and silently matching on a
    key nobody reads would hide it.  ``frame_matches`` on the HTTP/2 side
    made the same choice for the same reason.

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

    Field names match :class:`~blackbull.fault_injection.scenario_h2.ScenarioH2Result`
    wherever the two mean the same thing, so a harness that reports on one
    half does not need a second spelling for the other.
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
    #: The client's request head, once one arrived.  HTTP/1.1-specific:
    #: HTTP/2 has no single "head" to capture, it has frames.
    request_head: bytes = b''
    #: Things a ``WaitForRequest(match=...)`` step read and passed over.
    #: Non-zero means the connection is **desynced** — HTTP/1.1 responses
    #: are positional, so a request the scenario skipped is one it can no
    #: longer answer.  Surfaced rather than inferred.  (The HTTP/2 half
    #: counts the same thing under the same name; there it is harmless,
    #: because streams are independent.)
    wait_skipped: int = 0
    #: One ``(match, matched)`` pair per :class:`ExpectRequest` step, in
    #: order — what the scenario assumed, and whether it held.
    expectations: list = field(default_factory=list)
    #: True when a ``HalfClose`` step actually shut down the write side.
    #: False both when no such step ran and when the transport refused it
    #: (TLS has no half-close), so a test can tell "did not ask" from
    #: "asked and it did not happen" — a silently skipped half-close
    #: otherwise reads as a pass.
    half_closed: bool = False


# ---------------------------------------------------------------------------
# JSON Lines serialisation — the same shape ``scenario_h2`` uses
# ---------------------------------------------------------------------------


def _step_to_dict(step) -> dict:
    if isinstance(step, WaitForRequest):
        return {'op': StepOpH1Server.WAIT_FOR_REQUEST, 'timeout': step.timeout,
                'match': step.match}
    if isinstance(step, ExpectRequest):
        return {'op': StepOpH1Server.EXPECT_REQUEST, 'timeout': step.timeout,
                'match': step.match}
    if isinstance(step, SendRawBytes):
        return {'op': StepOpH1Server.SEND_RAW,
                'data': step.data.hex(),
                'byte_interval': step.byte_interval}
    if isinstance(step, Sleep):
        return {'op': StepOpH1Server.SLEEP, 'duration': step.duration}
    if isinstance(step, Abort):
        return {'op': StepOpH1Server.ABORT}
    if isinstance(step, CloseGracefully):
        return {'op': StepOpH1Server.CLOSE_GRACEFULLY}
    if isinstance(step, HalfClose):
        return {'op': StepOpH1Server.HALF_CLOSE}
    raise ValueError(f'cannot serialise step: {step!r}')


def _normalise_match(match: dict | None) -> dict:
    """JSON has no tuples, so ``header``'s pair comes back as a list.

    Normalised on the way in rather than compared loosely on the way out:
    a scenario that round-trips must equal the one it came from, and
    ``('x', 'y') != ['x', 'y']``.
    """
    match = dict(match or {})
    if 'header' in match and isinstance(match['header'], list):
        match['header'] = tuple(match['header'])
    return match


def _step_from_dict(d: dict):
    op = d['op']
    if op == StepOpH1Server.WAIT_FOR_REQUEST:
        return WaitForRequest(timeout=d.get('timeout', 5.0),
                              match=_normalise_match(d.get('match')))
    if op == StepOpH1Server.EXPECT_REQUEST:
        return ExpectRequest(timeout=d.get('timeout', 5.0),
                             match=_normalise_match(d.get('match')))
    if op == StepOpH1Server.SEND_RAW:
        return SendRawBytes(data=bytes.fromhex(d['data']),
                            byte_interval=d.get('byte_interval', 0.0))
    if op == StepOpH1Server.SLEEP:
        return Sleep(duration=d['duration'])
    if op == StepOpH1Server.ABORT:
        return Abort()
    if op == StepOpH1Server.CLOSE_GRACEFULLY:
        return CloseGracefully()
    if op == StepOpH1Server.HALF_CLOSE:
        return HalfClose()
    raise ValueError(f'unknown step op: {op!r}')


def scenario_to_json(scenario: ScenarioH1Server) -> str:
    """Serialise *scenario* to JSON Lines (one step per line).

    The name sits on the first line under the op ``HEADER``, so the file is
    one line-oriented stream with no out-of-band metadata — the convention
    :func:`blackbull.fault_injection.scenario_h2.scenario_to_json` set.

    Payloads are hex rather than base64 or an escaped string: a fault
    scenario's bytes are frequently *not* valid UTF-8 and are meant to be
    read by a human comparing them against a packet capture.
    """
    lines = [json.dumps({'op': 'HEADER', 'name': scenario.name})]
    for step in scenario.steps:
        lines.append(json.dumps(_step_to_dict(step)))
    return '\n'.join(lines)


def scenario_from_json(src: str) -> ScenarioH1Server:
    """Parse JSON Lines back to a :class:`ScenarioH1Server`."""
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
    return ScenarioH1Server(steps=tuple(steps), name=name)


# ---------------------------------------------------------------------------
# Byte assembly for the typed steps
# ---------------------------------------------------------------------------
#
# Here rather than in the production sender, for the reason the module
# docstring gives: a breaker that shares the production serialiser cannot
# emit a fault that serialiser has.


def encode_status_line(step: SendStatusLine) -> bytes:
    """``HTTP/1.1 200 OK\\r\\n``, or whatever the step says instead."""
    head = f'{step.version} {step.code}'
    if not step.omit_reason:
        head += f' {step.reason}'
    return head.encode('latin-1') + b'\r\n'


def encode_header(step: SendHeader) -> bytes:
    """One header line, or an obs-fold continuation of the previous one."""
    if step.fold:
        return b' ' + step.value.encode('latin-1') + b'\r\n'
    return (step.name.encode('latin-1') + b': '
            + step.value.encode('latin-1') + b'\r\n')


def encode_chunk(step: SendChunk) -> bytes:
    """One chunk, with the size line free to disagree with the data."""
    size = step.declared_size
    if size is None:
        size = len(step.data)
    line = format(size, 'x')
    if step.extension:
        line += f';{step.extension}'
    return line.encode('latin-1') + b'\r\n' + step.data + step.terminator


def encode_chunked_terminator(step: EndChunkedBody) -> bytes:
    """The zero chunk, plus any trailer fields."""
    out = b'0\r\n'
    for name, value in step.trailers:
        out += name.encode('latin-1') + b': ' + value.encode('latin-1') + b'\r\n'
    return out + b'\r\n'
