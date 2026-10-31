"""Deprecation shim — moved to [`blackbull.fault_injection`][blackbull.fault_injection].

Groups the HTTP/1.1 client-side scenario model and the
HTTP/2 server-side fault-injection surface under
[`blackbull.fault_injection`][blackbull.fault_injection].  Import from there going forward::

    from blackbull.fault_injection import Scenario, H1CSendRawBytes  # ...

This shim re-exports the names that used to live here (BBD003).
"""
from ..deprecations import _warn_deprecated

from blackbull.fault_injection.scenario_h1 import (  # noqa: F401
    Abort,
    ReadResponse,
    Scenario,
    ScenarioResult,
    SendRawBytes as SendBytes,
    Sleep,
    Step,
    StepOp,
)

_warn_deprecated('BBD003', 'blackbull.client.scenario')

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
