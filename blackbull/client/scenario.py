"""Deprecation shim — moved to :mod:`blackbull.fault_injection`.

Sprint 46 grouped the HTTP/1.1 client-side scenario model and the new
HTTP/2 server-side fault-injection surface under
:mod:`blackbull.fault_injection`.  Import from there going forward::

    from blackbull.fault_injection import Scenario, SendBytes  # ...

This shim re-exports the names that used to live here and will be
removed no earlier than BlackBull v0.45.0 (and not before
2026-09-16) — three releases ahead AND one month, whichever is later,
per the project's deprecation policy.
"""
import warnings as _warnings

from blackbull.fault_injection.scenario_h1 import (  # noqa: F401
    Abort,
    ReadResponse,
    Scenario,
    ScenarioResult,
    SendBytes,
    Sleep,
    Step,
    StepOp,
)

_warnings.warn(
    "blackbull.client.scenario is deprecated; import from "
    "blackbull.fault_injection instead.  The old path will be removed "
    "no earlier than BlackBull v0.45.0 (and not before 2026-09-16).",
    DeprecationWarning,
    stacklevel=2,
)

__all__ = [
    "Abort",
    "ReadResponse",
    "Scenario",
    "ScenarioResult",
    "SendBytes",
    "Sleep",
    "Step",
    "StepOp",
]
