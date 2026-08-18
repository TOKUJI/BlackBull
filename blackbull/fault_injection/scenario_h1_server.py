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
from dataclasses import dataclass, field


class StepOpH1Server(str, enum.Enum):
    """Tag used by the JSON serialiser."""
    WAIT_FOR_REQUEST = 'WAIT_FOR_REQUEST'
    SEND_RAW = 'SEND_RAW'
    SLEEP = 'SLEEP'
    ABORT = 'ABORT'
    CLOSE = 'CLOSE'


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
class CloseConnection:
    """Close cleanly (FIN) after whatever has been written.

    Terminal.  The difference from :class:`Abort` is what the client sees
    — an orderly EOF mid-body rather than a reset — and clients do not
    always treat the two alike, which is the point of having both.
    """


Step = WaitForRequest | SendRawBytes | Sleep | Abort | CloseConnection


@dataclass(frozen=True)
class ScenarioH1Server:
    """An ordered list of steps, plus a name for test parametrisation."""
    steps: list = field(default_factory=list)
    name: str = ''


@dataclass
class ScenarioH1ServerResult:
    """What the executor observed while running a scenario."""
    #: Steps that completed, in order.
    completed: list = field(default_factory=list)
    #: True when a ``WaitForRequest`` step expired without a request head.
    request_timed_out: bool = False
    #: The client's request head, once one arrived.
    request_head: bytes = b''
    #: Total bytes written at the client.
    bytes_sent: int = 0
    #: Seconds from first connection to the last step.
    elapsed: float = 0.0
