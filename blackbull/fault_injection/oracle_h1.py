"""Differential HTTP/1.1 oracle.

Compare normalized outcomes under a per-side timeout. A divergence is not
automatically a protocol defect; inspect its category and RFC requirement.
"""
from __future__ import annotations

import asyncio
import enum
import time
from dataclasses import dataclass

from .scenario_h1 import Scenario, ScenarioResult

# HTTP1Client is imported lazily inside run_scenario to break the
# import cycle: blackbull.client re-exports Scenario for back-compat
# and is therefore not safe to import at top level here.


class Category(str, enum.Enum):
    """Why a differential example was (not) accepted.

    Subclassing ``str`` makes the values JSON-serialisable directly
    and keeps ``assert ctx.category == 'OK'``-style sites readable.
    """
    OK = 'OK'                          # normalised responses match
    STATUS_DIFFER = 'STATUS_DIFFER'    # both responded, status differs
    BODY_DIFFER = 'BODY_DIFFER'        # both responded, same status, body differs
    HEADER_DIFFER = 'HEADER_DIFFER'    # both responded, same status+body, headers differ
    BB_TRANSPORT_FAIL = 'BB_TRANSPORT_FAIL'   # BlackBull errored, nginx fine
    NG_TRANSPORT_FAIL = 'NG_TRANSPORT_FAIL'   # nginx errored, BlackBull fine
    BB_TIMEOUT = 'BB_TIMEOUT'          # BlackBull stayed silent, nginx did not
    NG_TIMEOUT = 'NG_TIMEOUT'          # nginx ditto
    BOTH_REJECTED = 'BOTH_REJECTED'    # same 4xx/5xx status, or both closed before responding
    INCONCLUSIVE = 'INCONCLUSIVE'      # no verdict: both silent, both unreadable, or a premise did not hold
    HARNESS_ERROR = 'HARNESS_ERROR'    # the harness itself failed on either side


# The whitelist of categories that count as "no divergence".  Adding
# a new known-divergence here is a config change, not a code change.
ACCEPTED_CATEGORIES: frozenset[Category] = frozenset({
    Category.OK,
    Category.BOTH_REJECTED,
})


class Observed(str, enum.Enum):
    """What one side's run established about the peer."""
    RESPONSE = 'RESPONSE'    # every read got a complete response
    CLOSED = 'CLOSED'        # the peer closed or reset, after any ``responses``
    BROKEN = 'BROKEN'        # a reply could not be read as a response
    TIMEOUT = 'TIMEOUT'      # a read, or the runner, ran out of time
    UNMET = 'UNMET'          # a WaitFor/Expect premise of the scenario did not hold
    NO_READ = 'NO_READ'      # the scenario read nothing, so there is no verdict
    ERROR = 'ERROR'          # the harness failed: no connection, or its own exception


@dataclass
class SideOutcome:
    """One side's observation in a differential pair."""
    observed: Observed
    response: dict | None = None        # the newest of ``responses``
    #: Every response read, normalised {status, headers, body}, in order.
    responses: tuple[dict, ...] = ()
    exception: str | None = None        # repr of what ended the run, if anything
    elapsed_s: float = 0.0
    #: The raw scenario result; ``None`` when the run never reached the peer.
    result: ScenarioResult | None = None

    @property
    def ok(self) -> bool:
        return self.response is not None

    @property
    def timed_out(self) -> bool:
        return self.observed is Observed.TIMEOUT


# Framing / transport headers stripped from the differential
# comparison.  Both nginx and BlackBull may legitimately emit (or omit)
# these depending on local policy; comparing them produces noise.
#   - date / server: clock + identity (varies per host)
#   - content-length: derived from body, already in body
#   - connection: HTTP/1.1 keep-alive is implicit; nginx emits the
#     header explicitly while BlackBull omits it.  Both legal per
#     RFC 9112 §9.1.
#   - keep-alive: nginx-specific timing hint (RFC 9112 deprecates it)
#   - transfer-encoding: framing detail, derived from body shape
_FRAMING_HEADERS = frozenset({
    b'date',
    b'server',
    b'content-length',
    b'connection',
    b'keep-alive',
    b'transfer-encoding',
})


def normalize_response(resp) -> dict:
    """Drop volatile / framing-only headers; preserve status + body."""
    return {
        'status': resp.status,
        'headers': sorted(
            (k.lower(), v)
            for k, v in resp.headers
            if k.lower() not in _FRAMING_HEADERS
        ),
        'body': resp.body,
    }


def _is_error_status(resp: dict) -> bool:
    """True for 4xx (client error) and 5xx (server error) responses.

    When both servers reject an input with any error status the
    response *bodies* (e.g. BlackBull's ``501 Not Implemented``
    plaintext vs nginx's HTML error page) are server-specific and not
    a meaningful divergence, so the pair collapses to BOTH_REJECTED
    when statuses match.
    """
    s = resp.get('status')
    return isinstance(s, int) and 400 <= s < 600


_NO_VERDICT = frozenset({Observed.UNMET, Observed.NO_READ})


def categorize(ng: SideOutcome, bb: SideOutcome) -> Category:
    """Bucket a differential example into a [`Category`][].

    Only outcomes both sides confirmed alike are accepted: a timeout, an
    unmet premise or a harness failure is never a rejection.
    """
    kinds = {ng.observed, bb.observed}
    if Observed.ERROR in kinds:
        return Category.HARNESS_ERROR
    if kinds & _NO_VERDICT or kinds in ({Observed.TIMEOUT}, {Observed.BROKEN}):
        return Category.INCONCLUSIVE
    if bb.observed is Observed.TIMEOUT:
        return Category.BB_TIMEOUT
    if ng.observed is Observed.TIMEOUT:
        return Category.NG_TIMEOUT
    for n, b in zip(ng.responses, bb.responses):
        differ = _differ(n, b)
        if differ is not None:
            return differ
    if len(bb.responses) < len(ng.responses) or bb.observed is Observed.BROKEN:
        return Category.BB_TRANSPORT_FAIL
    if len(ng.responses) < len(bb.responses) or ng.observed is Observed.BROKEN:
        return Category.NG_TRANSPORT_FAIL
    if bb.observed is not ng.observed:
        return (Category.BB_TRANSPORT_FAIL if bb.observed is Observed.CLOSED
                else Category.NG_TRANSPORT_FAIL)
    if kinds == {Observed.CLOSED} or _is_error_status(bb.response):
        return Category.BOTH_REJECTED
    return Category.OK


def _differ(ng: dict, bb: dict) -> Category | None:
    """How two responses to the same request differ; ``None`` if alike.

    Matching error statuses are one rejection; mismatched error codes are a
    real divergence of validation policy.
    """
    if ng['status'] != bb['status']:
        return Category.STATUS_DIFFER
    if _is_error_status(ng):
        return None
    if ng['body'] != bb['body']:
        return Category.BODY_DIFFER
    if ng['headers'] != bb['headers']:
        return Category.HEADER_DIFFER
    return None


# Per-side wall-clock budget, so one pathological example cannot push a
# sweep past its budget.  Expiry is observed as TIMEOUT.
PER_REQUEST_TIMEOUT_S = 5.0


def _observed(result: ScenarioResult) -> Observed:
    if any(not held for _, held in result.expectations) or result.wait_timed_out:
        return Observed.UNMET
    if result.timed_out:
        return Observed.TIMEOUT
    if result.peer_closed:
        return Observed.CLOSED
    if result.exception is not None:
        return Observed.BROKEN
    return Observed.RESPONSE if result.received else Observed.NO_READ


async def run_scenario(host: str, port: int,
                       scenario: Scenario) -> tuple[SideOutcome, bytes]:
    """Execute *scenario* against (host, port) and return (outcome, wire_bytes).

    Returns the captured wire bytes (both servers receive identical
    bytes, so a single capture is enough for failure diagnostics).
    """
    from blackbull.client.http1 import HTTP1Client  # lazy; see module docstring

    t0 = time.monotonic()
    wire = b''
    try:
        async with HTTP1Client(host, port,
                               record_wire_bytes=True,
                               connect_timeout=2.0) as c:
            try:
                result = await asyncio.wait_for(
                    c.execute_scenario(scenario),
                    timeout=PER_REQUEST_TIMEOUT_S,
                )
            except TimeoutError as exc:
                return SideOutcome(Observed.TIMEOUT, exception=repr(exc),
                                   elapsed_s=time.monotonic() - t0), c.wire_buffer
            wire = c.wire_buffer
    except Exception as exc:  # noqa: BLE001 — no connection, or the harness's own fault
        return SideOutcome(Observed.ERROR, exception=repr(exc),
                           elapsed_s=time.monotonic() - t0), wire
    responses = tuple(normalize_response(r) for r in result.received)
    return SideOutcome(
        _observed(result),
        response=responses[-1] if responses else None,
        responses=responses,
        exception=result.exception,
        elapsed_s=time.monotonic() - t0,
        result=result,
    ), wire


__all__ = [
    'ACCEPTED_CATEGORIES',
    'Category',
    'Observed',
    'PER_REQUEST_TIMEOUT_S',
    'SideOutcome',
    'categorize',
    'normalize_response',
    'run_scenario',
]
