"""Differential oracle shared by the conformance test and atheris fuzz.

Sprint 18 Phase 3 — the categoriser, per-side outcome dataclass, and
scenario runner used to live inside
``tests/conformance/http1/test_http1_differential.py``.  That kept
them out of the production package (correct for test-only code), but
it also meant the atheris fuzz harness at
``tests/conformance/http1/fuzz/fuzz_http1.py`` couldn't reuse them
without importing the pytest module (which side-effects on import via
``pytest.importorskip`` and ``pytest.skip``).

This module is the smallest extracted surface that lets atheris drive
the same differential check as the Hypothesis test:

  * :class:`Category` — the 9-way enum that buckets each example.
  * :data:`ACCEPTED_CATEGORIES` — pass-through set (``OK``,
    ``BOTH_REJECTED``); divergences sit outside.
  * :class:`SideOutcome` — what one server returned (response, or
    exception / timeout) in normalised form.
  * :func:`normalize_response`, :func:`categorize` — the pure
    functions consumed by both callers.
  * :func:`run_scenario` — the async wrapper that drives a
    :class:`~blackbull.client.Scenario` against ``(host, port)`` and
    returns ``(SideOutcome, wire_bytes)``.
  * :data:`PER_REQUEST_TIMEOUT_S` — wall budget cap per side.

The pytest module continues to own everything *test-specific* —
fixtures, Hypothesis strategies, corpus capture, ``DiffContext`` —
since none of that is meaningful in the atheris context.
"""
from __future__ import annotations

import asyncio
import enum
import time
from dataclasses import dataclass

from .http1 import HTTP1Client
from .scenario import Scenario


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
    BB_TIMEOUT = 'BB_TIMEOUT'          # BlackBull exceeded per-example wait_for
    NG_TIMEOUT = 'NG_TIMEOUT'          # nginx ditto
    BOTH_REJECTED = 'BOTH_REJECTED'    # both rejected — 4xx/5xx with matching status, or both transport-failed


# The whitelist of categories that count as "no divergence".  Adding
# a new known-divergence here is a config change, not a code change.
ACCEPTED_CATEGORIES: frozenset[Category] = frozenset({
    Category.OK,
    Category.BOTH_REJECTED,
})


@dataclass
class SideOutcome:
    """One side's response in a differential pair.

    Exactly one of (response, exception) is populated.  ``timed_out``
    is True if the failure was an :class:`asyncio.TimeoutError` from
    the per-example wait_for; we record it separately because timeouts
    are semantically distinct from other transport errors.
    """
    response: dict | None = None        # normalised {status, headers, body}
    exception: str | None = None        # repr(exc) when no response arrived
    timed_out: bool = False
    elapsed_s: float = 0.0

    @property
    def ok(self) -> bool:
        return self.response is not None


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


def categorize(ng: SideOutcome, bb: SideOutcome) -> Category:
    """Bucket a differential example into a :class:`Category`.

    Order of the checks matters: both-rejected wins over individual
    transport failures so we don't flag inputs that nginx also
    refused.
    """
    # Both sides failed transport-wise (any mix of exception / timeout).
    if not ng.ok and not bb.ok:
        return Category.BOTH_REJECTED

    # One side responded, the other didn't.
    if ng.ok and not bb.ok:
        return Category.BB_TIMEOUT if bb.timed_out else Category.BB_TRANSPORT_FAIL
    if bb.ok and not ng.ok:
        return Category.NG_TIMEOUT if ng.timed_out else Category.NG_TRANSPORT_FAIL

    # Both responded with an HTTP status.  Both-error-status with
    # matching code → BOTH_REJECTED; mismatched error codes are a real
    # divergence of validation policy and stay STATUS_DIFFER.
    assert ng.response is not None and bb.response is not None
    if _is_error_status(ng.response) and _is_error_status(bb.response):
        if ng.response['status'] == bb.response['status']:
            return Category.BOTH_REJECTED

    if ng.response['status'] != bb.response['status']:
        return Category.STATUS_DIFFER
    if ng.response['body'] != bb.response['body']:
        return Category.BODY_DIFFER
    if ng.response['headers'] != bb.response['headers']:
        return Category.HEADER_DIFFER
    return Category.OK


# Per-side wall-clock budget.  Set so a single pathological example
# can't push a multi-example sweep past its budget; on timeout the
# categoriser folds the outcome into BB_TIMEOUT / NG_TIMEOUT rather
# than failing the run.
PER_REQUEST_TIMEOUT_S = 5.0


async def run_scenario(host: str, port: int,
                       scenario: Scenario) -> tuple[SideOutcome, bytes]:
    """Execute *scenario* against (host, port) and return (outcome, wire_bytes).

    Drives the scenario through :meth:`HTTP1Client.execute_scenario`,
    which itself never raises; this wrapper only adds an outer
    ``asyncio.wait_for`` so a runaway scenario (e.g. trickled bytes
    plus a long read timeout) can't blow the per-side budget.

    Returns the captured wire bytes (both servers receive identical
    bytes, so a single capture is enough for failure diagnostics).
    """
    t0 = time.monotonic()
    wire = b''
    try:
        async with HTTP1Client(host, port,
                               record_wire_bytes=True,
                               connect_timeout=2.0) as c:
            result = await asyncio.wait_for(
                c.execute_scenario(scenario),
                timeout=PER_REQUEST_TIMEOUT_S,
            )
            wire = c.wire_buffer
        if result.response is not None:
            return SideOutcome(
                response=normalize_response(result.response),
                elapsed_s=time.monotonic() - t0,
            ), wire
        if result.timed_out:
            return SideOutcome(
                exception=result.exception or 'TimeoutError',
                timed_out=True,
                elapsed_s=time.monotonic() - t0,
            ), wire
        # No response, no per-step timeout — either the scenario had
        # no ReadResponse step (treat as "no answer") or a primitive
        # raised mid-scenario.
        return SideOutcome(
            exception=result.exception or 'no-response (scenario had no READ step)',
            elapsed_s=time.monotonic() - t0,
        ), wire
    except asyncio.TimeoutError as exc:
        return SideOutcome(
            exception=repr(exc),
            timed_out=True,
            elapsed_s=time.monotonic() - t0,
        ), wire
    except Exception as exc:  # noqa: BLE001
        return SideOutcome(
            exception=repr(exc),
            elapsed_s=time.monotonic() - t0,
        ), wire


__all__ = [
    'ACCEPTED_CATEGORIES',
    'Category',
    'PER_REQUEST_TIMEOUT_S',
    'SideOutcome',
    'categorize',
    'normalize_response',
    'run_scenario',
]
