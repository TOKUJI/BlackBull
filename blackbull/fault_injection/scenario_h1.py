"""HTTP/1.1 client-side fault scenarios for a target server.

JSON Lines preserves typed steps. from_bytes is a total decoder for fuzz
inputs: malformed input must still map to a scenario rather than crash parsing.
"""
import enum
from dataclasses import dataclass, field
from typing import ClassVar, Union

from . import _scenario
from ._scenario import Abort, HalfClose, ReadResponse, SendRawBytes, Sleep


class StepOp(str, enum.Enum):
    """The v1 JSON tags, still read until v1 reading is removed."""
    SEND = 'SEND'
    SLEEP = 'SLEEP'
    READ = 'READ'
    ABORT = 'ABORT'
    HALF_CLOSE = 'HALF_CLOSE'
    WAIT_FOR_RESPONSE = 'WAIT_FOR_RESPONSE'
    EXPECT_RESPONSE = 'EXPECT_RESPONSE'


class WaitForResponse(_scenario._WaitFor):
    """Read responses until one satisfies ``match``, or the timeout wins.

    On HTTP/1.1 a non-zero ``wait_skipped`` is worth reading: responses
    arrive in request order, so skipping one means the scenario is a
    response further along than it thinks.
    """


class ExpectResponse(_scenario._Expect):
    """Read one response and record whether it matched."""


Step = Union[SendRawBytes, Sleep, ReadResponse, Abort, HalfClose,
             WaitForResponse, ExpectResponse]


@dataclass(frozen=True)
class Scenario:
    """A sequence of steps the executor walks against one connection."""
    steps: tuple[Step, ...]
    #: For test parametrisation and reporting, as the other three scenario
    #: types carry — so a failing CI run can name the scenario that broke.
    name: str = ''

    # ------------------------------------------------------------------
    # Convenience builders
    # ------------------------------------------------------------------

    @classmethod
    def well_formed(cls, raw_request: bytes, *,
                    response_timeout: float = 5.0) -> 'Scenario':
        """Wrap a complete raw HTTP/1.1 request as a one-shot scenario.

        Equivalent to "send these bytes, then read one response".
        Used by the Hypothesis ``well_formed_scenario_strategy`` in the
        HTTP/1.1 differential suite.
        """
        return cls(steps=(SendRawBytes(data=raw_request),
                          ReadResponse(timeout=response_timeout)))

    _v1: ClassVar = (True, {
        StepOp.SEND: 'SendRawBytes', StepOp.SLEEP: 'Sleep',
        StepOp.READ: 'ReadResponse', StepOp.ABORT: 'Abort',
        StepOp.HALF_CLOSE: 'HalfClose',
        StepOp.WAIT_FOR_RESPONSE: 'WaitForResponse',
        StepOp.EXPECT_RESPONSE: 'ExpectResponse'})

    def to_json(self) -> str:
        """Serialise to JSON Lines (see ``docs/guide/fault_injection.md``)."""
        return _scenario.to_json(self)

    @classmethod
    def from_json(cls, src: str) -> 'Scenario':
        """Parse what [`to_json`][Scenario.to_json] wrote."""
        return _scenario.from_json(cls, src)

    # ------------------------------------------------------------------
    # Bytes ↔ scenario — total decoder for atheris
    # ------------------------------------------------------------------

    @classmethod
    def from_bytes(cls, raw: bytes) -> 'Scenario':
        """Decode every byte string into a runnable fuzz scenario, including empty input.

        Truncated input returns the completed steps. Abort discards remaining bytes;
        SEND payload lengths are uint16. This decoder must not raise on arbitrary input.
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
    """Collect scenario outcomes; fields are not mutually exclusive.

    A timeout may precede a later response; response is newest, received holds all.
    """

    # Populated when a ReadResponse step received a full HTTP/1.1
    # response.  Typed loosely as object to avoid the import cycle
    # with blackbull.client.http2.ClientResponse — the executor sets
    # it to the real ClientResponse.
    response: object | None = None

    # repr of a peer or transport failure other than a close; the
    # harness's own exceptions propagate.
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
    #: True on successful write-side half-close; False if absent or unsupported.
    half_closed: bool = False
    # All read responses in order; response remains the newest.
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
    return scenario.to_json()


def scenario_from_json(src: str) -> Scenario:
    """Parse what [`Scenario.to_json`][] produced.  Twin of the other three."""
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
    'Sleep',
    'Step',
    'StepOp',
]

# ``SendRawBytes`` is the canonical spelling in all four scenario
# vocabularies; ``SendBytes`` is the deprecated client-side one.

# Unannotated: see tests/unit/test_deprecated_send_bytes_spellings.py::test_the_warning_is_attributed_to_the_callers_line.
def __getattr__(name):
    """Deprecated SendBytes alias for SendRawBytes; removal no earlier than 2027-08-19.
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
