"""Named fault-scenario builders.

Builders perform no I/O and allocate no scenarios at import. Returned
scenarios are immutable; use them in parametrized resilience tests.
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

# Catalogues distinguish protocol and client/server role; legacy aliases remain supported.
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
