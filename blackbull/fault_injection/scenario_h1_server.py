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
    SEND_RAW = 'SEND_RAW'
    SLEEP = 'SLEEP'
    ABORT = 'ABORT'
    CLOSE_GRACEFULLY = 'CLOSE_GRACEFULLY'


@dataclass(frozen=True)
class WaitForRequest:
    """Block until the client's request head has arrived (CRLFCRLF).

    A scenario that writes before the request is read is testing a
    different thing — an unsolicited response — and can simply omit this
    step.  On ``timeout`` expiry the executor records the miss and
    proceeds, matching ``WaitForClientFrame`` on the HTTP/2 side.
    """
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


Step = WaitForRequest | SendRawBytes | Sleep | Abort | CloseGracefully


@dataclass(frozen=True)
class ScenarioH1Server:
    """An ordered sequence of steps, plus a name for test parametrisation.

    ``steps`` is a **tuple**, matching :class:`ScenarioH2` — a frozen
    dataclass holding a mutable list is a frozen container of mutable
    contents, and the HTTP/2 half settled the question first.
    """
    steps: tuple[Step, ...] = ()
    name: str = ''


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


# ---------------------------------------------------------------------------
# JSON Lines serialisation — the same shape ``scenario_h2`` uses
# ---------------------------------------------------------------------------


def _step_to_dict(step) -> dict:
    if isinstance(step, WaitForRequest):
        return {'op': StepOpH1Server.WAIT_FOR_REQUEST, 'timeout': step.timeout}
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
    raise ValueError(f'cannot serialise step: {step!r}')


def _step_from_dict(d: dict):
    op = d['op']
    if op == StepOpH1Server.WAIT_FOR_REQUEST:
        return WaitForRequest(timeout=d.get('timeout', 5.0))
    if op == StepOpH1Server.SEND_RAW:
        return SendRawBytes(data=bytes.fromhex(d['data']),
                            byte_interval=d.get('byte_interval', 0.0))
    if op == StepOpH1Server.SLEEP:
        return Sleep(duration=d['duration'])
    if op == StepOpH1Server.ABORT:
        return Abort()
    if op == StepOpH1Server.CLOSE_GRACEFULLY:
        return CloseGracefully()
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
