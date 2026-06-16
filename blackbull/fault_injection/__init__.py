"""BlackBull's deliberate-misbehaviour toolkit.

A single namespace for the two directions of protocol fault injection:

* **Client-side, HTTP/1.1** — :mod:`blackbull.fault_injection.scenario_h1`
  ships a programmable client (driven through
  :meth:`blackbull.client.HTTP1Client.execute_scenario`) that emits
  deliberately bad HTTP/1.1 against a target *server*: trickled bytes,
  partial headers, mid-request idle, abrupt RST.
  :mod:`blackbull.fault_injection.oracle_h1` adds a differential oracle
  for comparing two HTTP/1.1 implementations under the same scenario.

* **Server-side, HTTP/2** — :mod:`blackbull.fault_injection.h2_server`
  ships a programmable server that emits deliberately bad HTTP/2 toward
  a target *client*: half-closed streams, exhausted windows, illegal
  SETTINGS, weird frame sequences.  A canned-misbehaviour catalogue
  lives at :mod:`blackbull.fault_injection.catalogue`.

This module is an opt-in testing instrument.  The HTTP/2 server refuses
to start when ``BB_PRODUCTION`` is set in the environment so a
deliberate-misbehaviour code path cannot accidentally fire on a
production deployment (see [`out-of-scope.md`](../../.claude/skills/update-roadmap/out-of-scope.md)).

See ``docs/guide/fault_injection.md`` for a tutorial.
"""
from __future__ import annotations

from .oracle_h1 import (
    ACCEPTED_CATEGORIES,
    PER_REQUEST_TIMEOUT_S,
    Category,
    SideOutcome,
    categorize,
    normalize_response,
    run_scenario,
)
from .scenario_h1 import (
    Abort,
    ReadResponse,
    Scenario,
    ScenarioResult,
    SendBytes,
    Sleep,
    Step,
    StepOp,
)

__all__ = [
    "ACCEPTED_CATEGORIES",
    "Abort",
    "Category",
    "PER_REQUEST_TIMEOUT_S",
    "ReadResponse",
    "Scenario",
    "ScenarioResult",
    "SendBytes",
    "SideOutcome",
    "Sleep",
    "Step",
    "StepOp",
    "categorize",
    "normalize_response",
    "run_scenario",
]
