#!/usr/bin/env python3
"""Where Sprint 103's responsibility split spends its instructions.

103 added **no calls per request** — the call-count diff is flat — so every
instruction it costs is inline, added to functions that already ran.  This
prints the per-function bytecode size on both sides so the growth can be
attributed to the specific plumbing the split introduced.

    BB_REPO=/tmp/bb-instr-base python split_cost.py
    BB_REPO=/tmp/bb-instr-103  python split_cost.py
"""
from __future__ import annotations

import dis
import os
import sys

REPO = os.environ.get('BB_REPO', '/home/toshio/work/BlackBull')
sys.meta_path = [f for f in sys.meta_path
                 if '__editable__' not in type(f).__module__
                 and '__editable__' not in getattr(f, '__name__', '')]
sys.path.insert(0, REPO)

import blackbull                                        # noqa: E402
assert blackbull.__file__.startswith(REPO), blackbull.__file__

from blackbull.server import connection_protocol as cp  # noqa: E402
from blackbull.server import read_buffer as rb          # noqa: E402


def n(fn):
    if fn is None:
        return None
    if isinstance(fn, property):
        fn = fn.fget
    return len(list(dis.get_instructions(fn)))


R, P, B = cp.BufferReader, cp.ConnectionProtocol, rb.ReadBuffer

TARGETS = [
    # the arrival path — runs once per transport delivery
    ('ConnectionProtocol.get_buffer', getattr(P, 'get_buffer', None)),
    ('ConnectionProtocol.buffer_updated', getattr(P, 'buffer_updated', None)),
    ('ReadBuffer.get_buffer', getattr(B, 'get_buffer', None)),
    ('ReadBuffer.buffer_updated', getattr(B, 'buffer_updated', None)),
    # the consuming path — runs once per read
    ('BufferReader.read', getattr(R, 'read', None)),
    ('BufferReader.readexactly', getattr(R, 'readexactly', None)),
    ('BufferReader.read_head', getattr(R, 'read_head', None)),
    ('BufferReader._consumed', getattr(R, '_consumed', None)),
    ('BufferReader.maybe_pause', getattr(R, 'maybe_pause', None)),
    ('BufferReader._at_boundary', getattr(R, '_at_boundary', None)),
    ('BufferReader.wait_for_data', getattr(R, 'wait_for_data', None)),
    ('ConnectionProtocol.maybe_resume', getattr(P, 'maybe_resume', None)),
    ('ConnectionProtocol.wait_for_data', getattr(P, 'wait_for_data', None)),
    ('ConnectionProtocol.wait_for_arrival', getattr(P, 'wait_for_arrival', None)),
    ('ConnectionProtocol.pause_reading', getattr(P, 'pause_reading', None)),
    ('ConnectionProtocol.resume_reading', getattr(P, 'resume_reading', None)),
    # buffer-side bookkeeping
    ('ReadBuffer.compact', getattr(B, 'compact', None)),
    ('ReadBuffer._release', getattr(B, '_release', None)),
    ('ReadBuffer.release_to_floor', getattr(B, 'release_to_floor', None)),
    ('ReadBuffer._make_room', getattr(B, '_make_room', None)),
    ('ReadBuffer.available', getattr(B, 'available', None)),
]

for label, fn in TARGETS:
    c = n(fn)
    print(f'{label:<40}{"-" if c is None else c}')
