"""Canned-misbehaviour catalogue for HTTP/2 client testing.

Each function returns a :class:`~blackbull.fault_injection.ScenarioH2`
that drives :class:`~blackbull.fault_injection.H2FaultServer` through
one well-known misbehaviour pattern: one named pathology per scenario,
so a suite can ``parametrize`` over the set.  The four spec-grade
categories they cover — half-closed streams, exhausted flow-control
windows, illegal SETTINGS, weird frame sequences — are tabulated in
``docs/guide/fault_injection.md`` against the builder for each.

Catalogue entries are pure builders.  They allocate nothing at
import time, take no I/O, and the returned scenario is immutable;
two consecutive calls to the same builder are interchangeable.
"""
from __future__ import annotations

from .h2 import (
    exhausted_window_zero_initial,
    half_closed_after_headers as h2_half_closed_after_headers,
    half_closed_stream_no_data,
    headers_continuation_dropped,
    settings_max_frame_size_below_minimum,
)

from .h1 import CATALOGUE as CATALOGUE_H1
from .h1_client import CATALOGUE as CATALOGUE_H1_CLIENT
from .h2_client import CATALOGUE as CATALOGUE_H2_CLIENT

#: The four cells, named by protocol **and role**.  The older spellings
#: below predate the grid being full: ``CATALOGUE`` and ``CATALOGUE_H2``
#: are the HTTP/2 *server* set, and ``CATALOGUE_H1`` the HTTP/1.1 *server*
#: set — names that read as "the HTTP/2 one" and were unambiguous only
#: while each protocol had a single cell.  Both spellings are exported;
#: the role-qualified four are what a reader should reach for.
CATALOGUE_H1_SERVER = CATALOGUE_H1

#: HTTP/2 cases.  ``CATALOGUE`` keeps its original name and contents so
#: existing ``parametrize`` over it is untouched; ``CATALOGUE_H2`` is the
#: symmetric alias, and ``CATALOGUE_H1`` the HTTP/1.1 set.  Two protocols,
#: two dicts, reachable the same way — an H1 set importable only from its
#: own module is how a reader concludes there is one catalogue.
CATALOGUE = {
    'half_closed_after_headers': h2_half_closed_after_headers,
    'half_closed_stream_no_data': half_closed_stream_no_data,
    'exhausted_window_zero_initial': exhausted_window_zero_initial,
    'settings_max_frame_size_below_minimum':
        settings_max_frame_size_below_minimum,
    'headers_continuation_dropped': headers_continuation_dropped,
}
CATALOGUE_H2 = CATALOGUE
CATALOGUE_H2_SERVER = CATALOGUE

#: Every cell, keyed the way the grid is drawn.  A suite that wants to
#: sweep the whole toolkit iterates this rather than remembering four
#: module paths — and a cell added later shows up without the suite
#: changing.
CATALOGUES = {
    'h1_client': CATALOGUE_H1_CLIENT,
    'h1_server': CATALOGUE_H1_SERVER,
    'h2_client': CATALOGUE_H2_CLIENT,
    'h2_server': CATALOGUE_H2_SERVER,
}

__all__ = [
    'CATALOGUE',
    'CATALOGUES',
    'CATALOGUE_H1',
    'CATALOGUE_H1_CLIENT',
    'CATALOGUE_H1_SERVER',
    'CATALOGUE_H2',
    'CATALOGUE_H2_CLIENT',
    'CATALOGUE_H2_SERVER',
    'exhausted_window_zero_initial',
    'half_closed_stream_no_data',
    'headers_continuation_dropped',
    'settings_max_frame_size_below_minimum',
]
