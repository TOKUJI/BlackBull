"""Programmable HTTP/1.1 wire-level scenario model.

A :class:`Scenario` is a sequence of typed *steps* that the
:meth:`blackbull.client.HTTP1Client.execute_scenario` executor walks
in order against a live socket.  This is the *client-side* half of
the :mod:`blackbull.fault_injection` toolkit: a programmable client
that drives a target HTTP/1.1 server through deliberate misbehaviour
— slowloris trickle, mid-request idle, abrupt RST, partial reads —
expressed as data, not procedural test code.

The symmetric *server-side* half (programmable HTTP/2 server emitting
deliberate misbehaviour toward a client) lives in
:mod:`blackbull.fault_injection.h2_server`.

Use cases:

  * Conformance differential testing — Hypothesis generates scenarios
    and :mod:`blackbull.fault_injection.oracle_h1` compares the target
    server's response to a reference (e.g. nginx).
  * Coverage-guided fuzzing — atheris's byte mutations decode into
    scenarios via :meth:`Scenario.from_bytes`.
  * **External callers** — server-library authors, proxy authors, and
    security researchers driving their server through programmable
    misbehaviour from a pytest suite.

Two serialisations are supported:

  * :meth:`Scenario.to_json` / :meth:`Scenario.from_json` — JSON Lines,
    one step per line.  Diff-friendly in git; readable when failures
    are pasted into reports.
  * :meth:`Scenario.from_bytes` — a *total* opcode-tagged decoder.
    Every byte string maps to a valid scenario, so atheris's byte-level
    mutations never crash on input parsing — each mutation produces a
    distinct execution path against the server.
"""
import base64
import enum
import json
from dataclasses import dataclass, field
from typing import Union


class StepOp(str, enum.Enum):
    """Tag for serialising / decoding a step.  ``str`` mixin so values
    drop straight into JSON without a custom encoder."""
    SEND = 'SEND'
    SLEEP = 'SLEEP'
    READ = 'READ'
    ABORT = 'ABORT'
    HALF_CLOSE = 'HALF_CLOSE'
    WAIT_FOR_RESPONSE = 'WAIT_FOR_RESPONSE'
    EXPECT_RESPONSE = 'EXPECT_RESPONSE'


@dataclass(frozen=True)
class SendRawBytes:
    """Push raw bytes onto the connection.

    ``byte_interval > 0`` transmits one byte at a time with that delay
    between bytes — the primitive slowloris-style stall that lets
    scenarios express trickled headers or trickled bodies without
    dropping to a raw asyncio socket.
    """
    data: bytes
    byte_interval: float = 0.0


@dataclass(frozen=True)
class Sleep:
    """Idle for ``duration`` seconds without sending or reading.

    Useful for post-headers idle, mid-keep-alive idle, and pre-response
    stall scenarios where the server is expected to time out and close.
    """
    duration: float


@dataclass(frozen=True)
class ReadResponse:
    """Read one HTTP/1.1 response from the connection.

    ``timeout`` bounds the entire status-line + headers + body read.
    On timeout the executor records the outcome on the
    :class:`ScenarioResult` and does *not* raise — the caller decides
    whether to treat that as a transport-fail or normal outcome.
    """
    timeout: float = 5.0


@dataclass(frozen=True)
class Abort:
    """Hard-close the connection (transport.abort → RST on Linux).

    Distinct from the graceful ``writer.close()`` / ``wait_closed()``
    in ``HTTP1Client.__aexit__``.  Subsequent steps short-circuit; the
    executor stops walking the scenario after an Abort.
    """


# Step is the discriminated union the executor switches on.  Listed
# as a Union (not StrEnum) because the dispatcher matches by isinstance,
# and frozen dataclasses are hashable / comparable / safe to share.
@dataclass(frozen=True)
class WaitForResponse:
    """Read responses until one satisfies ``match``, or the timeout wins.

    A **filter**: non-matching responses are read, counted in
    ``wait_skipped``, and passed over.  The role-axis twin of
    :class:`~blackbull.fault_injection.scenario_h1_server.WaitForRequest`,
    and it exists for the same reason — a scenario that has to know
    exactly how many messages precede the interesting one is a scenario
    written against a particular peer.

    On HTTP/1.1 a non-zero ``wait_skipped`` is worth reading: responses
    arrive in request order, so skipping one means the scenario is a
    response further along than it thinks.
    """
    match: dict = field(default_factory=dict)
    timeout: float = 5.0


@dataclass(frozen=True)
class ExpectResponse:
    """Read one response and record whether it matched.

    A **guard**, not a filter: nothing is skipped and the executor moves
    on either way.  It answers *is the peer behaving as this scenario
    assumes?* — and a scenario whose premise silently failed would
    otherwise look like a pass.

    Twin of
    :class:`~blackbull.fault_injection.scenario_h1_server.ExpectRequest`.
    """
    match: dict = field(default_factory=dict)
    timeout: float = 5.0


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


Step = Union[SendRawBytes, Sleep, ReadResponse, Abort, HalfClose,
             WaitForResponse, ExpectResponse]


@dataclass(frozen=True)
class Scenario:
    """A sequence of steps the executor walks against one connection."""
    steps: tuple[Step, ...]
    #: For test parametrisation and reporting, as the other three scenario
    #: types carry.  Added by the 107+108 consistency sweep: a scenario a
    #: failing CI run can point at by name is worth two lines.
    name: str = ''

    # ------------------------------------------------------------------
    # Convenience builders
    # ------------------------------------------------------------------

    @classmethod
    def well_formed(cls, raw_request: bytes, *,
                    response_timeout: float = 5.0) -> 'Scenario':
        """Wrap a complete raw HTTP/1.1 request as a one-shot scenario.

        Equivalent to "send these bytes, then read one response".
        Used by the legacy ``diff_*.txt`` corpus loader and by the
        Hypothesis ``well_formed_scenario_strategy``.
        """
        return cls(steps=(SendRawBytes(data=raw_request),
                          ReadResponse(timeout=response_timeout)))

    # ------------------------------------------------------------------
    # JSON Lines serialisation — one step per line
    # ------------------------------------------------------------------

    def to_json(self) -> str:
        """Serialise to JSON Lines: one ``{"op": ..., ...}`` per line.

        Bytes payloads are base64-encoded so the result round-trips
        through stdout / git / json.loads without escape ambiguity.
        Round-tripped by :meth:`from_json`.

        The scenario's name rides the first line under the op ``HEADER``,
        the convention the other three vocabularies use, so the file stays
        one line-oriented stream with no out-of-band metadata.  Without it
        a round trip silently dropped the name, and a catalogue case that
        came back anonymous cannot say which case it is.
        """
        lines = [json.dumps({'op': 'HEADER', 'name': self.name})]
        for step in self.steps:
            lines.append(json.dumps(_step_to_dict(step)))
        return '\n'.join(lines)

    @classmethod
    def from_json(cls, src: str) -> 'Scenario':
        """Parse JSON Lines back to a :class:`Scenario`.

        Skips blank lines so files that end with a trailing newline
        (the conventional git-friendly shape) parse cleanly.

        A ``HEADER`` line carries the name.  It is optional on the way in:
        corpus files written before the header existed have no such line
        and still parse, yielding an unnamed scenario.
        """
        name = ''
        steps: list[Step] = []
        for line in src.splitlines():
            line = line.strip()
            if not line:
                continue
            d = json.loads(line)
            if d.get('op') == 'HEADER':
                name = d.get('name', '')
                continue
            steps.append(_step_from_dict(d))
        return cls(steps=tuple(steps), name=name)

    # ------------------------------------------------------------------
    # Bytes ↔ scenario — total decoder for atheris
    # ------------------------------------------------------------------

    @classmethod
    def from_bytes(cls, raw: bytes) -> 'Scenario':
        """Decode arbitrary bytes into a scenario.

        Total function: *every* byte string yields a valid scenario,
        including the empty string (→ empty scenario).  Designed so
        atheris's coverage-guided byte mutations always produce
        runnable input — the fuzzer never spends cycles on parser
        errors.

        Encoding:

          * The decoder walks ``raw`` left-to-right.  At each
            position the next byte selects an opcode via ``% 4``
            (every byte value is therefore a legal opcode tag).
          * Each opcode then consumes a small payload from the
            following bytes.  If the payload is short (end of input),
            decoding stops cleanly and the partial scenario is
            returned.

        Opcode layout::

            byte % 4 == 0  → SEND
                next 2 bytes (big-endian uint16) = length;
                next ``length`` bytes = data;
                next 1 byte (% len(_BYTE_INTERVAL_TABLE))
                  → byte_interval.
            byte % 4 == 1  → SLEEP
                next 1 byte (% len(_SLEEP_TABLE)) → duration.
            byte % 4 == 2  → READ
                next 1 byte (% len(_TIMEOUT_TABLE)) → timeout.
            byte % 4 == 3  → ABORT
                no payload.  Remaining bytes are discarded — an
                Abort short-circuits execution anyway, so it's the
                natural terminator.

        Bounded payload sizes (uint16 length) keep individual
        scenarios well under 64 KiB, which is what we want for
        per-iteration fuzz throughput.
        """
        steps: list[Step] = []
        i = 0
        n = len(raw)
        while i < n:
            opcode = raw[i] % 4
            i += 1
            if opcode == 0:  # SEND
                if i + 2 > n:
                    break
                length = (raw[i] << 8) | raw[i + 1]
                i += 2
                if i + length > n:
                    # Short read — take what's left as the SEND payload.
                    length = n - i
                data = bytes(raw[i:i + length])
                i += length
                if i < n:
                    bi = _BYTE_INTERVAL_TABLE[raw[i] % len(_BYTE_INTERVAL_TABLE)]
                    i += 1
                else:
                    bi = 0.0
                steps.append(SendRawBytes(data=data, byte_interval=bi))
            elif opcode == 1:  # SLEEP
                if i >= n:
                    steps.append(Sleep(duration=_SLEEP_TABLE[0]))
                    break
                duration = _SLEEP_TABLE[raw[i] % len(_SLEEP_TABLE)]
                i += 1
                steps.append(Sleep(duration=duration))
            elif opcode == 2:  # READ
                if i >= n:
                    steps.append(ReadResponse(timeout=_TIMEOUT_TABLE[0]))
                    break
                timeout = _TIMEOUT_TABLE[raw[i] % len(_TIMEOUT_TABLE)]
                i += 1
                steps.append(ReadResponse(timeout=timeout))
            else:  # ABORT
                steps.append(Abort())
                break
        return cls(steps=tuple(steps))


# Fixed tables — the bytes decoder maps a raw byte to one of these via
# modulo.  Small + deterministic so the same byte always produces the
# same scenario shape (atheris coverage requires this).
_BYTE_INTERVAL_TABLE: tuple[float, ...] = (0.0, 0.0, 0.05, 0.2)
_SLEEP_TABLE: tuple[float, ...] = (0.05, 0.25, 1.0, 2.0)
_TIMEOUT_TABLE: tuple[float, ...] = (0.5, 1.0, 2.0, 5.0)


# ----------------------------------------------------------------------
# Result object — what HTTP1Client.execute_scenario returns
# ----------------------------------------------------------------------


@dataclass
class ScenarioResult:
    """Outcome of one :meth:`HTTP1Client.execute_scenario` call.

    Exactly one of ``response`` / ``exception`` / ``timed_out`` /
    ``aborted`` is the meaningful field; the others are ``None`` /
    ``False``.  The executor never raises, so callers (differential
    test, fuzz harness) categorise on this object instead of writing
    try/except boilerplate per scenario.
    """

    # Populated when a ReadResponse step received a full HTTP/1.1
    # response.  Typed loosely as object to avoid the import cycle
    # with blackbull.client.http2.ClientResponse — the executor sets
    # it to the real ClientResponse.
    response: object | None = None

    # Set when a step raised.  Stored as the repr to keep the
    # dataclass picklable for cross-process diagnostics.
    exception: str | None = None

    # True when a ReadResponse step hit its per-step timeout.
    timed_out: bool = False

    # True when execution stopped because an Abort step ran.
    aborted: bool = False

    # 0-based count of steps that ran to completion (excluding the
    # one that failed, timed out, or aborted).  Helpful when bisecting
    # which step in a long scenario caused a regression.
    steps_completed: int = 0

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


# ----------------------------------------------------------------------
# JSON helpers — kept module-private so the public surface is the
# Scenario classmethods.
# ----------------------------------------------------------------------


def _step_to_dict(step: Step) -> dict:
    if isinstance(step, SendRawBytes):
        return {
            'op': StepOp.SEND.value,
            'data': base64.b64encode(step.data).decode('ascii'),
            'byte_interval': step.byte_interval,
        }
    if isinstance(step, Sleep):
        return {'op': StepOp.SLEEP.value, 'duration': step.duration}
    if isinstance(step, ReadResponse):
        return {'op': StepOp.READ.value, 'timeout': step.timeout}
    if isinstance(step, Abort):
        return {'op': StepOp.ABORT.value}
    if isinstance(step, HalfClose):
        return {'op': StepOp.HALF_CLOSE.value}
    if isinstance(step, WaitForResponse):
        return {'op': StepOp.WAIT_FOR_RESPONSE.value,
                'timeout': step.timeout, 'match': dict(step.match)}
    if isinstance(step, ExpectResponse):
        return {'op': StepOp.EXPECT_RESPONSE.value,
                'timeout': step.timeout, 'match': dict(step.match)}
    raise TypeError(f'unknown step type: {type(step).__name__}')


def _normalise_match(match: dict | None) -> dict:
    """JSON has no tuples, so ``header``'s pair comes back as a list.

    Normalised on the way in rather than compared loosely on the way out:
    a scenario that round-trips must equal the one it came from, and
    ``('x', 'y') != ['x', 'y']``.  The HTTP/1.1 server half does the same.
    """
    match = dict(match or {})
    if 'header' in match and isinstance(match['header'], list):
        match['header'] = tuple(match['header'])
    return match


def _step_from_dict(d: dict) -> Step:
    op = d.get('op')
    if op == StepOp.SEND.value:
        return SendRawBytes(
            data=base64.b64decode(d['data']),
            byte_interval=float(d.get('byte_interval', 0.0)),
        )
    if op == StepOp.SLEEP.value:
        return Sleep(duration=float(d['duration']))
    if op == StepOp.READ.value:
        return ReadResponse(timeout=float(d.get('timeout', 5.0)))
    if op == StepOp.ABORT.value:
        return Abort()
    if op == StepOp.HALF_CLOSE.value:
        return HalfClose()
    if op == StepOp.WAIT_FOR_RESPONSE.value:
        return WaitForResponse(timeout=float(d.get('timeout', 5.0)),
                               match=_normalise_match(d.get('match')))
    if op == StepOp.EXPECT_RESPONSE.value:
        return ExpectResponse(timeout=float(d.get('timeout', 5.0)),
                              match=_normalise_match(d.get('match')))
    raise ValueError(f'unknown step op: {op!r}')


def response_matches(response, match: dict) -> bool:
    """Return True iff *response* satisfies every key in *match*.

    Recognised keys: ``status``, ``reason``, ``version``, ``header``,
    ``header_absent``, ``body_contains``.  **Unknown keys fail closed** —
    an unrecognised key is almost certainly a typo in a scenario, and
    silently matching on a key nobody reads would hide it.
    ``request_matches`` and ``frame_matches`` made the same choice for the
    same reason.

    ``header`` takes ``(name, value)``; pass ``value=None`` to match on
    presence alone.  ``header_absent`` takes a name.
    """
    recognised = {'status', 'reason', 'version', 'header', 'header_absent',
                  'body_contains'}
    if set(match) - recognised:
        return False
    if not match:
        return True

    for key in ('status', 'reason', 'version'):
        if key in match and getattr(response, key, None) != match[key]:
            return False

    def _name(n):
        return n.lower() if isinstance(n, str) else bytes(n).lower()

    headers = list(getattr(response, 'headers', ()) or ())
    names = {_name(n) for n, _ in headers}
    if 'header' in match:
        name, value = match['header']
        name = _name(name)
        if value is None:
            if name not in names:
                return False
        elif not any(_name(n) == name and v == value for n, v in headers):
            return False
    if 'header_absent' in match and _name(match['header_absent']) in names:
        return False
    if 'body_contains' in match:
        body = getattr(response, 'body', b'') or b''
        if match['body_contains'] not in body:
            return False
    return True


def scenario_to_json(scenario: Scenario) -> str:
    """Serialise *scenario* to JSON Lines (one step per line).

    The same free function the other three vocabularies expose.  This cell
    shipped first and grew ``Scenario.to_json`` as a method; the method
    stays, because callers use it, but a reader comparing the four files
    should not be told they differ where they do not.
    """
    return scenario.to_json()


def scenario_from_json(src: str) -> Scenario:
    """Parse what :func:`scenario_to_json` produced.  Twin of the other three."""
    return Scenario.from_json(src)


__all__ = [
    'Abort',
    'ExpectResponse',
    'HalfClose',
    'WaitForResponse',
    'response_matches',
    'scenario_from_json',
    'scenario_to_json',
    'ReadResponse',
    'Scenario',
    'ScenarioResult',
    'SendBytes',
    'Sleep',
    'Step',
    'StepOp',
]

# ---------------------------------------------------------------------------
# Naming: ``SendRawBytes`` is the canonical spelling across all four
# vocabularies
# ---------------------------------------------------------------------------
#
# The two server-side vocabularies have always called this ``SendRawBytes``
# and the two client-side ones ``SendRawBytes`` — the same step, the same two
# fields, the name split by *role* rather than by anything a reader could
# predict.  The consistency sweep at the 107+108 close found it, and with a
# typed alternative now present on every half, "raw" is the word that earns
# its place.
#
# ``SendRawBytes`` is the name to use.  ``SendRawBytes`` keeps working and is
# **deprecated**: removal no earlier than 2027-08-19, and at an arbitrary
# time after that, following the deprecation window ASGI uses.

def __getattr__(name: str):
    """PEP 562 — warn when the deprecated spelling is actually used.

    A module-level assignment would alias silently; going through
    ``__getattr__`` means a reader who never touches ``SendRawBytes`` never
    sees a warning, and one who does gets it at their own call site.
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
