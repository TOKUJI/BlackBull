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
from .h1_server import (
    H1FaultServer,
    H1FaultServerError,
)
# Role-qualified, because three scenario vocabularies share step names and a
# bare ``SendRawBytes`` silently resolved to HTTP/2's — handing an
# ``H1FaultServer`` a step it rejects as unknown.  The ``H1S`` prefix reads as
# "HTTP/1.1 server-side", the half these belong to; ``scenario_h1``'s
# unprefixed ``Abort``/``Sleep`` are the *client* side and keep their names,
# as ``scenario_h2``'s keep ``H2``.
from .scenario_h1_server import (
    Abort as H1SAbort,
    CloseGracefully as H1SCloseGracefully,
    ScenarioH1Server,
    ScenarioH1ServerResult,
    SendRawBytes as H1SSendRawBytes,
    Sleep as H1SSleep,
    StepOpH1Server,
    WaitForRequest,
    scenario_from_json as scenario_h1_server_from_json,
    scenario_to_json as scenario_h1_server_to_json,
)
# Fourth vocabulary, fourth set of role-qualified aliases.  ``H2C`` reads as
# "HTTP/2 client-side"; the unprefixed names stay with the HTTP/1.1 client
# vocabulary that had them first, ``H1S`` is the HTTP/1.1 server side and
# ``H2`` the HTTP/2 server side.
from .scenario_h2_client import (
    Abort as H2CAbort,
    CLIENT_PREFACE as H2C_CLIENT_PREFACE,
    ReadResponse as H2CReadResponse,
    ScenarioH2Client,
    ScenarioH2ClientResult,
    SendBytes as H2CSendBytes,
    SendFrame as H2CSendFrame,
    SendPreface as H2CSendPreface,
    Sleep as H2CSleep,
    StepOpH2Client,
    scenario_from_json as scenario_h2_client_from_json,
    scenario_to_json as scenario_h2_client_to_json,
)
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
    "H1FaultServer",
    "H1SAbort",
    "H1SCloseGracefully",
    "H1SSendRawBytes",
    "H1SSleep",
    "H2Abort",
    "H2CAbort",
    "H2CReadResponse",
    "H2CSendBytes",
    "H2CSendFrame",
    "H2CSendPreface",
    "H2CSleep",
    "H2C_CLIENT_PREFACE",
    "H2FaultServer",
    "H2FaultServerError",
    "H2Sleep",
    "H2Step",
    "PER_REQUEST_TIMEOUT_S",
    "ReadResponse",
    "Scenario",
    "ScenarioH1Server",
    "StepOpH1Server",
    "StepOpH2Client",
    "ScenarioH1ServerResult",
    "ScenarioH2",
    "ScenarioH2Client",
    "ScenarioH2ClientResult",
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
    "WaitForRequest",
    "categorize",
    "frame_matches",
    "make_self_signed_h2_context",
    "normalize_response",
    "run_scenario",
    "scenario_h1_server_from_json",
    "scenario_h1_server_to_json",
    "scenario_h2_client_from_json",
    "scenario_h2_client_to_json",
    "scenario_h2_from_json",
    "scenario_h2_to_json",
    "serialize_frame",
]
