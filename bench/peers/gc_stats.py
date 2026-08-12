"""GC observation sampler for the Sprint 100 Phase 1 mean-vs-tail fork.

Observation-only: a daemon thread samples ``gc.get_stats()`` every
``BB_GC_STATS_INTERVAL`` seconds and appends one JSON line per snapshot to
the file named by ``BB_GC_STATS_OUT``.  It does NOT wrap or time any
request-path method — it adds no boundary to the per-request path, so a
whole-server measurement stays whole-server (the one-split-point rule).

Why a file, not an endpoint: a debug endpoint on the server under test
changes what is measured (extra route dispatch + response on every probe);
an out-of-process sampler cannot read another process's ``gc`` counters.
The daemon thread is the only shape that observes the real server process
without touching the request path.  Overhead is one ``gc.get_stats()``
read + one small file append per interval, in a thread the event loop
never awaits.

Snapshot line (one JSON object):
    {"t": <monotonic seconds>, "col0": n, "col1": n, "col2": n,
     "obj0": n, "obj1": n, "obj2": n, "uncollectable": n}

gen i: "colN" = ``gc.get_stats()[i]["collections"]`` (collections
performed), "objN" = ``gc.get_stats()[i]["collected"]`` (objects
reclaimed).  Deltas across a load window are computed offline by
``phase1_analysis.py`` — never by this module.

Activation: set ``BB_GC_STATS_OUT=<file>`` before importing the app; the
app imports this module env-gated.  A baseline line is written at import
(before any load); the thread appends every interval.  Off by default.
"""
import gc
import json
import os
import threading
import time

_OUT = os.environ.get("BB_GC_STATS_OUT", "")
# Multi-worker (SO_REUSEPORT) safety: every worker would truncate the same
# file with mode "w" and interleave lines; scope each worker's file by pid.
if _OUT and os.environ.get("BB_WORKERS", "1") != "1":
    _OUT = f"{_OUT}.{os.getpid()}"
_ACTIVE = bool(_OUT)
_INTERVAL = float(os.environ.get("BB_GC_STATS_INTERVAL", "2"))


def _snapshot() -> dict:
    stats = gc.get_stats()
    return {
        "t": time.monotonic(),
        "col0": stats[0]["collections"],
        "col1": stats[1]["collections"],
        "col2": stats[2]["collections"],
        "obj0": stats[0]["collected"],
        "obj1": stats[1]["collected"],
        "obj2": stats[2]["collected"],
        "uncollectable": stats[2].get("uncollectable", 0),
    }


def _writer() -> None:
    while True:
        time.sleep(_INTERVAL)
        try:
            with open(_OUT, "a") as fh:
                fh.write(json.dumps(_snapshot()) + "\n")
        except OSError:
            pass


_started = False


def activate() -> None:
    global _started
    if not _ACTIVE or _started:
        return
    # Baseline line at import — before any load begins.
    try:
        with open(_OUT, "w") as fh:
            fh.write(json.dumps(_snapshot()) + "\n")
    except OSError:
        pass
    threading.Thread(target=_writer, daemon=True).start()
    _started = True


activate()
