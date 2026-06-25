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
from dataclasses import dataclass
from typing import Union


class StepOp(str, enum.Enum):
    """Tag for serialising / decoding a step.  ``str`` mixin so values
    drop straight into JSON without a custom encoder."""
    SEND = 'SEND'
    SLEEP = 'SLEEP'
    READ = 'READ'
    ABORT = 'ABORT'


@dataclass(frozen=True)
class SendBytes:
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
Step = Union[SendBytes, Sleep, ReadResponse, Abort]


@dataclass(frozen=True)
class Scenario:
    """A sequence of steps the executor walks against one connection."""
    steps: tuple[Step, ...]

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
        return cls(steps=(SendBytes(data=raw_request),
                          ReadResponse(timeout=response_timeout)))

    # ------------------------------------------------------------------
    # JSON Lines serialisation — one step per line
    # ------------------------------------------------------------------

    def to_json(self) -> str:
        """Serialise to JSON Lines: one ``{"op": ..., ...}`` per line.

        Bytes payloads are base64-encoded so the result round-trips
        through stdout / git / json.loads without escape ambiguity.
        Round-tripped by :meth:`from_json`.
        """
        lines = []
        for step in self.steps:
            lines.append(json.dumps(_step_to_dict(step)))
        return '\n'.join(lines)

    @classmethod
    def from_json(cls, src: str) -> 'Scenario':
        """Parse JSON Lines back to a :class:`Scenario`.

        Skips blank lines so files that end with a trailing newline
        (the conventional git-friendly shape) parse cleanly.
        """
        steps: list[Step] = []
        for line in src.splitlines():
            line = line.strip()
            if not line:
                continue
            steps.append(_step_from_dict(json.loads(line)))
        return cls(steps=tuple(steps))

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
                steps.append(SendBytes(data=data, byte_interval=bi))
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


# ----------------------------------------------------------------------
# JSON helpers — kept module-private so the public surface is the
# Scenario classmethods.
# ----------------------------------------------------------------------


def _step_to_dict(step: Step) -> dict:
    if isinstance(step, SendBytes):
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
    raise TypeError(f'unknown step type: {type(step).__name__}')


def _step_from_dict(d: dict) -> Step:
    op = d.get('op')
    if op == StepOp.SEND.value:
        return SendBytes(
            data=base64.b64decode(d['data']),
            byte_interval=float(d.get('byte_interval', 0.0)),
        )
    if op == StepOp.SLEEP.value:
        return Sleep(duration=float(d['duration']))
    if op == StepOp.READ.value:
        return ReadResponse(timeout=float(d.get('timeout', 5.0)))
    if op == StepOp.ABORT.value:
        return Abort()
    raise ValueError(f'unknown step op: {op!r}')


__all__ = [
    'Abort',
    'ReadResponse',
    'Scenario',
    'ScenarioResult',
    'SendBytes',
    'Sleep',
    'Step',
    'StepOp',
]
