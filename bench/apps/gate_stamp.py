"""Armed-state gate stamp (Sprint 100 Phase 2 F3+ review fix).

The bare calibration arm proves nothing on its own: it strips every timing
env, so the instrument module is never imported and "bare" is asserted, never
verified — which is how a contaminated bare arm (responly < bare is physically
impossible) can go unnoticed.  This module is imported by BOTH bench apps on
EVERY launch and writes what was actually armed to ``BB_GATE_STAMP_OUT``, so
every arm — including bare — can prove itself from the analysis side.

Env-gated; off by default (no ``BB_GATE_STAMP_OUT`` → no-op).
"""
import os


def write_gate_stamp() -> None:
    out = os.environ.get("BB_GATE_STAMP_OUT", "")
    if not out:
        return
    resp = 1 if os.environ.get("BB_RESP_TIMING_OUT") else 0
    handler = 1 if os.environ.get("BB_HANDLER_TIMING") else 0
    parse = 1 if os.environ.get("BB_PARSE_TIMING_OUT") else 0
    dispatch = 1 if os.environ.get("BB_DISPATCH_TIMING_OUT") else 0
    read = 1 if os.environ.get("BB_READ_TIMING_OUT") else 0
    try:
        with open(out, "w") as fh:
            fh.write(f"resp={resp} handler={handler} parse={parse} "
                     f"dispatch={dispatch} read={read}\n")
    except OSError:
        pass


write_gate_stamp()
