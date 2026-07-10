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
to start in a production context — when ``BLACKBULL_ENV=production`` (the
framework's production signal) or the explicit ``BB_PRODUCTION`` override
is set — so a deliberate-misbehaviour code path cannot accidentally fire on
a production deployment.

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
from ._tls import make_self_signed_h2_context
from .h2_server import (
    CLIENT_PREFACE,
    H2FaultServer,
    H2FaultServerError,
    serialize_frame,
)
from .scenario_h2 import (
    Abort as H2Abort,
    CloseGracefully,
    H2Step,
    ScenarioH2,
    ScenarioH2Result,
    SendFrame,
    SendRawBytes,
    Sleep as H2Sleep,
    StepOpH2,
    WaitForClientFrame,
    frame_matches,
    scenario_from_json as scenario_h2_from_json,
    scenario_to_json as scenario_h2_to_json,
)

__all__ = [
    "ACCEPTED_CATEGORIES",
    "Abort",
    "CLIENT_PREFACE",
    "Category",
    "CloseGracefully",
    "H2Abort",
    "H2FaultServer",
    "H2FaultServerError",
    "H2Sleep",
    "H2Step",
    "PER_REQUEST_TIMEOUT_S",
    "ReadResponse",
    "Scenario",
    "ScenarioH2",
    "ScenarioH2Result",
    "ScenarioResult",
    "SendBytes",
    "SendFrame",
    "SendRawBytes",
    "SideOutcome",
    "Sleep",
    "Step",
    "StepOp",
    "StepOpH2",
    "WaitForClientFrame",
    "categorize",
    "frame_matches",
    "make_self_signed_h2_context",
    "normalize_response",
    "run_scenario",
    "scenario_h2_from_json",
    "scenario_h2_to_json",
    "serialize_frame",
]
