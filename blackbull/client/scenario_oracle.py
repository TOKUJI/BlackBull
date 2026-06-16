"""Deprecation shim — moved to :mod:`blackbull.fault_injection`.

Sprint 46 grouped the HTTP/1.1 differential oracle alongside the
scenario model under :mod:`blackbull.fault_injection`.  Import from
there going forward::

    from blackbull.fault_injection import Category, run_scenario  # ...

This shim re-exports the names that used to live here and will be
removed no earlier than BlackBull v0.45.0 (and not before
2026-09-16) — three releases ahead AND one month, whichever is later,
per the project's deprecation policy.
"""
import warnings as _warnings

from blackbull.fault_injection.oracle_h1 import (  # noqa: F401
    ACCEPTED_CATEGORIES,
    PER_REQUEST_TIMEOUT_S,
    Category,
    SideOutcome,
    categorize,
    normalize_response,
    run_scenario,
)

_warnings.warn(
    "blackbull.client.scenario_oracle is deprecated; import from "
    "blackbull.fault_injection instead.  The old path will be removed "
    "no earlier than BlackBull v0.45.0 (and not before 2026-09-16).",
    DeprecationWarning,
    stacklevel=2,
)

__all__ = [
    "ACCEPTED_CATEGORIES",
    "Category",
    "PER_REQUEST_TIMEOUT_S",
    "SideOutcome",
    "categorize",
    "normalize_response",
    "run_scenario",
]
