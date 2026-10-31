"""Every deprecation BlackBull announces, and the one way it warns about one.

``docs/reference/deprecations.md`` publishes [`table`][]; a test keeps the page
and this registry equal.
"""
from __future__ import annotations

import warnings
from typing import NamedTuple


class BlackBullDeprecationWarning(DeprecationWarning):
    """A BlackBull feature that is going away; the message names its entry."""


class Deprecation(NamedTuple):
    subject: str
    replacement: str
    since: str
    removal: str
    issue: str
    #: False for an announcement with nothing to warn from at runtime.
    warns: bool = True


DEPRECATIONS: dict[str, Deprecation] = {
    'BBD001': Deprecation(
        'blackbull.middleware.CacheMiddleware, CompressionMiddleware and '
        'TrustedProxyMiddleware', 'Cache, Compression and TrustedProxy',
        'v0.28.0', 'no earlier than 2027-05-20', 'BLA-581'),
    'BBD002': Deprecation(
        'the pre-built blackbull.middleware.compress instance',
        'an explicit Compression()', 'v0.28.0', 'no earlier than 2027-05-20',
        'BLA-581'),
    'BBD003': Deprecation(
        'blackbull.client.scenario and blackbull.client.scenario_oracle',
        'blackbull.fault_injection', 'v0.42.0', 'in v0.82.0', 'BLA-578'),
    'BBD004': Deprecation(
        'blackbull.Request', 'blackbull.Connection, which has the same members',
        'v0.60.0', 'no earlier than 2027-08-01', 'BLA-582'),
    'BBD005': Deprecation(
        'SendBytes and H2CSendBytes',
        'SendRawBytes (H1CSendRawBytes and H2CSendRawBytes in '
        'blackbull.fault_injection)', 'v0.78.0', 'no earlier than 2027-08-19',
        'BLA-583'),
    'BBD006': Deprecation(
        'scenario JSON in the v1 format', 'the current format: to_json(from_json(text))',
        'v0.81.1', 'no earlier than v0.84.0 and 2026-11-28', 'BLA-584'),
    'BBD007': Deprecation(
        'the encode_* helpers of scenario_h1_server and scenario_h2_client',
        "each step's wire()", 'v0.81.1', 'no earlier than v0.84.0 and 2026-11-28',
        'BLA-584'),
    'BBD008': Deprecation(
        'the BLACKBULL_* environment variables', 'the same names with the BB_ prefix',
        'v0.81.1', 'no earlier than October 2027', 'BLA-559', warns=False),
    'BBD009': Deprecation(
        'Python 3.11 support', 'Python 3.12 or later', 'v0.81.1',
        'in the first release from October 2027', 'BLA-586', warns=False),
}


# Unannotated: see tests/unit/test_deprecated_send_bytes_spellings.py::test_the_warning_is_attributed_to_the_callers_line.
def _warn_deprecated(key, what=None, instead=None, stacklevel=2):
    """Warn about entry *key*; *stacklevel* counts from the caller, as in ``warnings.warn``.

    *what* and *instead* name the spelling the caller reached, when it is
    narrower than the entry.
    """
    entry = DEPRECATIONS[key]
    warnings.warn(
        f'{what or entry.subject} is deprecated; use {instead or entry.replacement}. '
        f'Removed {entry.removal} ({key}).',
        BlackBullDeprecationWarning, stacklevel=stacklevel + 1)


def table() -> str:
    """The registry as the Markdown table the deprecations page publishes."""
    rows = ['| ID | Deprecated | Use instead | Since | Removal | Warns | Issue |',
            '|---|---|---|---|---|---|---|']
    rows += [f'| {key} | {e.subject} | {e.replacement} | {e.since} | {e.removal} '
             f'| {"yes" if e.warns else "no"} | {e.issue} |'
             for key, e in DEPRECATIONS.items()]
    return '\n'.join(rows)
