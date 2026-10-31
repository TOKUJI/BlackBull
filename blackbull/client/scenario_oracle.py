"""Deprecation shim — moved to [`blackbull.fault_injection`][blackbull.fault_injection].

Groups the HTTP/1.1 differential oracle alongside the
scenario model under [`blackbull.fault_injection`][blackbull.fault_injection].  Import from
there going forward::

    from blackbull.fault_injection import Category, run_scenario  # ...

This shim re-exports the names that used to live here (BBD003).
"""
from ..deprecations import _warn_deprecated

from blackbull.fault_injection.oracle_h1 import (  # noqa: F401
    ACCEPTED_CATEGORIES,
    PER_REQUEST_TIMEOUT_S,
    Category,
    SideOutcome,
    categorize,
    normalize_response,
    run_scenario,
)

_warn_deprecated('BBD003', 'blackbull.client.scenario_oracle')

__all__ = [
    "ACCEPTED_CATEGORIES",
    "Category",
    "PER_REQUEST_TIMEOUT_S",
    "SideOutcome",
    "categorize",
    "normalize_response",
    "run_scenario",
]
