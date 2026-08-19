"""Canned-misbehaviour catalogue for HTTP/2 client testing.

Each function returns a :class:`~blackbull.fault_injection.ScenarioH2`
that drives :class:`~blackbull.fault_injection.H2FaultServer` through
one well-known misbehaviour pattern.  Catalogue scenarios are
deliberately small — one named pathology per scenario — so test
suites can stack ``parametrize`` over the catalogue to assert
client-side resilience across the four spec-grade categories the
roadmap calls out:

  * **Half-closed streams** — server stops mid-stream without
    END_STREAM / RST_STREAM; client must time out or give up.
  * **Exhausted flow-control windows** — server advertises a
    zero-byte window then refuses to grant WINDOW_UPDATE; client
    must respect backpressure rather than spin.
  * **Custom / illegal SETTINGS** — server advertises a value
    below the RFC-mandated minimum or an unknown setting id;
    client must treat as PROTOCOL_ERROR per RFC 9113 §6.5.2.
  * **Weird frame sequences** — server emits frames in an
    out-of-order or unfinished pattern (HEADERS without
    END_HEADERS and no CONTINUATION, DATA on stream 0); client
    must close the connection with PROTOCOL_ERROR.

Catalogue entries are pure builders.  They allocate nothing at
import time, take no I/O, and the returned scenario is immutable;
two consecutive calls to the same builder are interchangeable.
"""
from __future__ import annotations

from .h2 import (
    exhausted_window_zero_initial,
    half_closed_stream_no_data,
    headers_continuation_dropped,
    settings_max_frame_size_below_minimum,
)

from .h1 import CATALOGUE as CATALOGUE_H1

#: HTTP/2 cases.  ``CATALOGUE`` keeps its original name and contents so
#: existing ``parametrize`` over it is untouched; ``CATALOGUE_H2`` is the
#: symmetric alias, and ``CATALOGUE_H1`` the HTTP/1.1 set.  Two protocols,
#: two dicts, reachable the same way — the H1 set was previously only
#: importable from its own module, which is how a reader would have
#: concluded there was one catalogue.
CATALOGUE = {
    'half_closed_stream_no_data': half_closed_stream_no_data,
    'exhausted_window_zero_initial': exhausted_window_zero_initial,
    'settings_max_frame_size_below_minimum':
        settings_max_frame_size_below_minimum,
    'headers_continuation_dropped': headers_continuation_dropped,
}
CATALOGUE_H2 = CATALOGUE

__all__ = [
    'CATALOGUE',
    'CATALOGUE_H1',
    'CATALOGUE_H2',
    'exhausted_window_zero_initial',
    'half_closed_stream_no_data',
    'headers_continuation_dropped',
    'settings_max_frame_size_below_minimum',
]
