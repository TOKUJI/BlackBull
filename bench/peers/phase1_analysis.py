#!/usr/bin/env python3
"""Sprint 100 Phase 1 — mean vs tail split summary.

Reads the peer-compare harness's own output (never re-measures) and prints
the two numbers the Phase 1 fork needs, per server:

1. wrk2 /plaintext latency distribution — p50 / p90 / p99 / p99.9 / p99.99
   / max from the CO-corrected fixed-rate B2r log (bench/wrk2/run.sh).
2. ``gc.get_stats()`` deltas over the whole run — per-generation collection
   counts and objects reclaimed, plus per-second rates — from the
   ``gc_<stack>.jsonl`` files the observation sampler wrote.

The fork verdict is read off the shape of the two columns across servers:
gap uniform across percentiles => steady per-request work (Phase 2 + 3a);
gap concentrated in the tail => event-driven (GC / allocation, Phase 3b).
This script only formats the numbers; the verdict is the reader's.

Usage:
    python bench/peers/phase1_analysis.py <scratch-or-results-dir>

The directory is the harness's SCRATCH dir (``results/aws/<ts>/results/
scratch_<ts>/`` after a bench/aws/run.sh pull) or anything containing
``wrk2_*_B2r_plaintext_rate*.txt`` and ``gc_*.jsonl`` files.
"""
import glob
import json
import re
import sys
from pathlib import Path

_PCT_KEYS = (50.0, 90.0, 99.0, 99.9, 99.99)


def _parse_wrk2(log_text: str) -> dict:
    out = {}
    for line in log_text.splitlines():
        m = re.match(r"^\s*([0-9.]+)%\s+([0-9.]+)([a-z\u00b5]+)", line)
        if m:
            out[float(m.group(1))] = (float(m.group(2)), m.group(3))
    avg = max_ = rps = None
    for line in log_text.splitlines():
        stripped = line.lstrip()
        if stripped.startswith("Requests/sec:"):
            rps = float(stripped.split(":")[1].split()[0])
        elif stripped.startswith("Latency") and re.match(r"^\s+\S", line):
            parts = stripped.split()
            # The Thread Stats row: "Latency <avg> <stdev> <max> <pct>%".
            # "Latency Distribution (HdrHistogram ...)" also starts with
            # "Latency" — require a time-valued second token to disambiguate.
            if len(parts) >= 4 and re.search(r"(ms|us|s)$", parts[1]):
                avg = parts[1]
                max_ = parts[3]
    return {"pcts": out, "avg": avg, "max": max_, "rps": rps}


def _parse_gc(path: Path) -> dict:
    rows = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if line:
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    if not rows:
        return {}
    first, last = rows[0], rows[-1]
    dt = last["t"] - first["t"]
    fields = ["col0", "col1", "col2", "obj0", "obj1", "obj2", "uncollectable"]
    totals = {f: last[f] - first[f] for f in fields}
    rates = {f: (v / dt if dt > 0 else 0.0) for f, v in totals.items()}
    return {"dt": dt, "totals": totals, "rates": rates}


def main(directory: str) -> None:
    root = Path(directory)
    print(f"# Phase 1 — mean vs tail split (Sprint 100)")
    print(f"_Source: {root.resolve()}_")
    print()

    print("## wrk2 /plaintext (CO-corrected, fixed rate)")
    print("| stack | rps | avg | p50 | p90 | p99 | p99.9 | p99.99 | max |")
    print("|---|---|---|---|---|---|---|---|---|")
    wrk2_files = sorted(glob.glob(str(root / "wrk2_*B2r_plaintext_rate*.txt")))
    for f in wrk2_files:
        stack = Path(f).name.split("wrk2_", 1)[1].split("_B2r", 1)[0]
        data = _parse_wrk2(Path(f).read_text())
        pcts = data["pcts"]
        cells = []
        for k in _PCT_KEYS:
            key = min(pcts, key=lambda x: abs(x - k)) if pcts else None
            if key is not None:
                val, unit = pcts[key]
                cells.append(f"{val:.3f}{unit}")
            else:
                cells.append("—")
        rps = f"{data['rps']:.0f}" if data["rps"] is not None else "—"
        avg = data["avg"] or "—"
        mx = data["max"] or "—"
        print(f"| {stack} | {rps} | {avg} | {' | '.join(cells)} | {mx} |")
    if not wrk2_files:
        print("| _(no wrk2 B2r logs found)_ |")
    print()

    print("## gc.get_stats() deltas over the run")
    print("| stack | dt(s) | col0 | col1 | col2 | obj0 | obj1 | obj2 | uncol |")
    print("|       |       | col/s | col/s | col/s | obj/s | obj/s | obj/s | |")
    print("|---|---|---|---|---|---|---|---|---|---|")
    gc_files = sorted(glob.glob(str(root / "gc_*.jsonl")))
    for f in gc_files:
        stack = Path(f).name[3:-6]  # gc_<stack>.jsonl
        stack = stack.rsplit(".", 1)[0]  # drop a possible <pid> (multi-worker)
        data = _parse_gc(Path(f))
        if not data:
            print(f"| {stack} | _(empty)_ |")
            continue
        t = data["totals"]
        r = data["rates"]
        print(
            f"| {stack} | {data['dt']:7.1f} "
            f"| {t['col0']:6d} | {t['col1']:6d} | {t['col2']:6d} "
            f"| {t['obj0']:8d} | {t['obj1']:8d} | {t['obj2']:8d} "
            f"| {t['uncollectable']:5d} |"
        )
        print(
            f"|       |       "
            f"| {r['col0']:6.2f} | {r['col1']:6.2f} | {r['col2']:6.2f} "
            f"| {r['obj0']:8.1f} | {r['obj1']:8.1f} | {r['obj2']:8.1f} | |"
        )
    if not gc_files:
        print("| _(no gc_*.jsonl files found)_ |")

    _f1_summary(root)
    _f2_summary(root)
    _calibration_summary(root)


def _f1_summary(root: Path) -> None:
    """Sprint 100 Phase 2 F1 fork: read-vs-response split in CPU-µs/req.

    Reads the harness's own Phase 2 captures (never re-measures):
    - ``cpu_<stack>.txt``  — server utime+stime across the measured lanes
      (``pid=N ticks=N clk_tck=N``);
    - ``resp_<stack>.txt`` — the response-transmit seam totals
      (``seam=.. calls=N sum_ms=.. mean_us=.. max_us=..``);
    - ``wrk_<stack>_*_run*.txt`` + ``wrk2_<stack>_*.txt`` — served requests
      (Requests/sec × Duration per scenario file).

    The fork numbers per server (at saturation, where per-request wall-clock
    ≈ CPU):
      total µs/req        = cpu_seconds / lane_requests * 1e6
      response-half µs/req = resp_sum_us / lane_requests   (wall-clock ≈ CPU)
      read+dispatch+handler(+render) µs/req = total − response-half

    Caveats (documented): the resp window spans the whole server life
    (incl. warmup, ~4 % extra requests), and BB's seam includes header render
    while sanic's excludes it (render is a later leaf).  Precision is reserved
    for the leaf microbench; this is the cheap fork measure.
    """
    print()
    print("## Phase 2 F1 — read-vs-response split (CPU-µs/req, saturation)")
    print("| stack | lane_reqs | total µs/req | resp-half µs/req | rest µs/req |")
    print("|---|---:|---:|---:|---:|")

    def lane_requests(stack: str) -> float:
        total = 0.0
        for f in glob.glob(str(root / f"wrk_{stack}_*_run*.txt")):
            total += _wrk_requests(Path(f))
        for f in glob.glob(str(root / f"wrk2_{stack}_*.txt")):
            total += _wrk_requests(Path(f))
        return total

    def _wrk_requests(path: Path) -> float:
        txt = path.read_text(errors="replace")
        m = re.search(r"Requests/sec:\s*([0-9.]+)", txt)
        if not m:
            return 0.0
        # Exact count when the log carries it ("N requests in T"); the
        # "Duration:" line the old regex matched never appears in wrk/wrk2
        # output, so it silently fell back to 30.0 — a latent mis-scale if
        # DURATION ever changes.
        x = re.search(r"(\d+) requests in ([0-9.]+)s", txt)
        if x:
            return float(x.group(1))
        return float(m.group(1)) * 30.0

    # Whole-lane capture only: cpu_<stack>.txt where the stack name has no
    # underscore — the per-scenario (cpu_<stack>_<scenario>.txt) and bare
    # calibration (cpu_<stack>_bare.txt) files have no whole-lane resp
    # counterpart and must not be treated as stacks.
    stacks = sorted(
        {p.name[4:-4] for p in root.glob("cpu_*.txt")
         if "_" not in p.name[4:-4]})  # cpu_<stack>.txt (whole lane)
    if not stacks:
        print("| _(no cpu_*.txt — Phase 2 F1 capture not enabled)_ |")
        return
    for stack in stacks:
        cpu = _parse_cpu(root / f"cpu_{stack}.txt")
        resp = _parse_resp(root / f"resp_{stack}.txt")
        reqs = lane_requests(stack)
        if reqs <= 0:
            print(f"| {stack} | _(no wrk logs to count requests)_ |")
            continue
        total_us = cpu["sec"] / reqs * 1e6 if cpu["sec"] else float("nan")
        resp_us = resp["sum_us"] / reqs if resp["sum_us"] else float("nan")
        rest_us = total_us - resp_us
        print(f"| {stack} | {reqs:10.0f} | {total_us:12.2f} "
              f"| {resp_us:15.2f} | {rest_us:10.2f} |")


def _f2_summary(root: Path) -> None:
    """Sprint 100 Phase 2 F2 fork: per-scenario handler split (CPU-µs/req).

    Reads the harness's per-scenario Phase 2 captures (never re-measures):
    - ``cpu_<stack>_<scenario>.txt``   — server utime+stime across the
      scenario's wrk runs (cpu0/cpu1 taken at the scenario boundaries);
    - ``resp_<stack>_<scenario>.txt``  — resp seam delta for the scenario;
    - ``handler_<stack>_<scenario>.txt`` — handler-region bracket delta.

    The scenario request count comes from the exact ``N requests in T`` in
    the wrk/wrk2 logs (no Duration-regex fallback).

    Quantities per stack per scenario (CPU-µs/req):
      total          = cpu_seconds / requests
      resp           = resp_seam / requests          (send path)
      handler        = handler bracket / requests
      handler_logic  = handler − resp for BlackBull (its simplified-handler
                       wrapper runs the send *inside* the bracket), else
                       handler (sanic sends after the handler returns)
      front          = total − resp − handler_logic  (read+parse+dispatch +
                       machinery + post-handler glue)

    The F2 verdict is read off B1: is the BB−sanic deficit in `front`
    (the read path) or in `handler_logic`?
    """
    print()
    print("## Phase 2 F2 — per-scenario handler split (CPU-µs/req)")

    def _parse_file(path: Path, pat: str, default: float = 0.0) -> float:
        try:
            m = re.search(pat, path.read_text(errors="replace"))
            return float(m.group(1)) if m else default
        except OSError:
            return default

    def scenario_requests(stack: str, scenario: str) -> float:
        total = 0.0
        for f in glob.glob(str(root / f"wrk_{stack}_{scenario}_run*.txt")):
            total += _wrk_requests(Path(f))
        for f in glob.glob(str(root / f"wrk2_{stack}_{scenario}.txt")):
            total += _wrk_requests(Path(f))
        return total

    def _wrk_requests(path: Path) -> float:
        txt = path.read_text(errors="replace")
        m = re.search(r"Requests/sec:\s*([0-9.]+)", txt)
        if not m:
            return 0.0
        x = re.search(r"(\d+) requests in ([0-9.]+)s", txt)
        if x:
            return float(x.group(1))
        return float(m.group(1)) * 30.0

    # Per-scenario capture files: resp_<stack>_<scenario>.txt (the whole-lane
    # resp_<stack>.txt has no scenario segment and is excluded by the glob;
    # the calibration captures resp_<stack>_bare/responly.txt are excluded
    # explicitly — they are not scenario runs).
    resp_files = sorted(
        f for f in glob.glob(str(root / "resp_*_*.txt"))
        if not f.endswith(("_bare.txt", "_responly.txt"))
    )
    if not resp_files:
        print("| _(no per-scenario resp_<stack>_<scenario>.txt — "
              "F2 capture not enabled)_ |")
        return

    rows = []
    for f in resp_files:
        name = Path(f).name
        rest = name[len("resp_"):-4]
        stack, _, scenario = rest.partition("_")
        cpu_s = _parse_file(root / f"cpu_{stack}_{scenario}.txt",
                            r"ticks=(\d+)\s+clk_tck=(\d+)", 0.0)
        clk = _parse_file(root / f"cpu_{stack}_{scenario}.txt",
                          r"clk_tck=(\d+)", 100.0) or 100.0
        cpu_us = cpu_s / clk * 1e6
        resp_us = _parse_file(Path(f), r"resp_sum_ms=([0-9.]+)") * 1000.0
        # The handler bracket is the F2 split point — a missing file means
        # BB_HANDLER_TIMING was not exported on the server (silent zero
        # would degrade sanic's front into the forbidden "other side
        # assumed zero").  Flag loudly instead of fabricating a number.
        handler_path = root / f"handler_{stack}_{scenario}.txt"
        if not handler_path.exists():
            print(f"| {stack} | {scenario} | _(MISSING handler_<stack>_<scenario>.txt — "
                  f"BB_HANDLER_TIMING not armed on server; F2 invalid)_ |")
            continue
        handler_us = _parse_file(handler_path,
                                 r"handler_sum_ms=([0-9.]+)") * 1000.0
        reqs = scenario_requests(stack, scenario)
        if reqs <= 0:
            continue
        total_us = cpu_us / reqs
        resp_r = resp_us / reqs
        handler_r = handler_us / reqs
        if stack.startswith("blackbull"):
            # BB's simplified-handler wrapper runs the send inside the
            # bracket; subtract resp for logic.  (startswith, not ==, so the
            # blackbull-cleartext variant keeps the same semantics.)
            handler_logic_r = handler_r - resp_r
        else:
            handler_logic_r = handler_r
        front_r = total_us - resp_r - handler_logic_r
        rows.append((stack, scenario, reqs, total_us, resp_r, handler_r,
                     handler_logic_r, front_r))

    if not rows:
        print("| _(no complete per-scenario captures)_ |")
        return

    print("| stack | scenario | reqs | total µs/req | resp | handler | "
          "handler_logic | front |")
    print("|---|---|---:|---:|---:|---:|---:|---:|")
    for r in sorted(rows):
        stack, scenario, reqs, total_us, resp_r, handler_r, hlog, front = r
        mark = "  ◀ B1 (F2 fork)" if scenario.startswith("B1_") else ""
        print(f"| {stack} | {scenario} | {reqs:9.0f} | {total_us:8.2f} "
              f"| {resp_r:7.2f} | {handler_r:7.2f} | {hlog:9.2f} "
              f"| {front:7.2f} |{mark}")

    # B1-only delta (the F2 fork's parent quantity is the B1 like-for-like
    # +7.43 µs/req, not the body-size-mixed whole-lane +8.87).
    def _b1(stack):
        for r in rows:
            if r[0] == stack and r[1].startswith("B1_"):
                return r
        return None

    bb, sa = _b1("blackbull"), _b1("sanic")
    if bb and sa:
        print()
        print("B1 delta BB−sanic (µs/req):")
        print(f"  total  = {bb[3] - sa[3]:+.2f}   (parent: +7.43 like-for-like)")
        print(f"  resp   = {bb[4] - sa[4]:+.2f}")
        print(f"  handler_logic = {bb[6] - sa[6]:+.2f}")
        print(f"  front  = {bb[7] - sa[7]:+.2f}   (read+parse+dispatch)")


def _calibration_summary(root: Path) -> None:
    """Sprint 100 instrument-cost calibration (per-seam, same instance).

    The timing instruments (resp seam + handler bracket, and the F3 parse
    seam once it exists) run inside the server process, so the measured
    totals include the instrument's own per-request cost.  compare_servers.sh
    runs CALIBRATE_RUNS B1s per calibration mode on the SAME instance —
    bare (no instruments), responly (resp seam only) and resphandler
    (resp + handler bracket, no parse seam) — and the full instrumented B1
    comes from the main bench.  Same-instance medians isolate each seam's
    cost:

      total instrument cost = full − bare
      resp-seam cost        = responly − bare
      handler-bracket cost  = resphandler − responly
      parse-seam cost       = full − resphandler   (0 until the F3 seam exists)

    Honesty check: in the F2 re-measurement (2026-08-11) the calibration
    arms were ALL identically instrumented — BB_TIMING_SNAP + BB_HANDLER_TIMING
    leaked into every arm via env inheritance, so bare/responly were
    instrumentally identical to full and every per-arm diff was run-to-run
    noise (~0.7 µs/req), not an instrument cost.  When the responly arm's
    writer output shows handler_calls > 0, this section flags the per-seam
    split as VOID and the per-arm diffs as noise.

    The resp/handler sub-term measurements time the real code path with
    thread_time(); the wrapper bookkeeping sits outside the timed window, so
    the instrument's CPU lands in the total (→ front), not in the sub-term
    measurements.  The corrected B1 row therefore subtracts the instrument
    from total (front absorbs it) and leaves resp/handler as measured.
    """
    print()
    print("## Instrument calibration (same-instance B1, CPU-µs/req)")
    print("| stack | full B1 | bare B1 | resp-only | resp+handler | resp-seam | "
          "handler-bracket | parse-seam | total instr |")
    print("|---|---:|---:|---:|---:|---:|---:|---:|---:|")

    def _exact_req(path: Path) -> float:
        txt = path.read_text(errors="replace")
        m = re.search(r"Requests/sec:\s*([0-9.]+)", txt)
        if not m:
            return 0.0
        x = re.search(r"(\d+) requests in ([0-9.]+)s", txt)
        return float(x.group(1)) if x else float(m.group(1)) * 30.0

    def _scenario_req(stack: str, scenario: str) -> float:
        total = 0.0
        for f in glob.glob(str(root / f"wrk_{stack}_{scenario}_run*.txt")):
            total += _exact_req(Path(f))
        for f in glob.glob(str(root / f"wrk2_{stack}_{scenario}.txt")):
            total += _exact_req(Path(f))
        return total

    def _cal_us(stack: str, tag: str, scenario: str):
        """Median µs/req across the calibration runs (cpu_<stack>_<tag>_runN.txt
        + wrk_<stack>_<tag>_<scenario>_runN.txt); falls back to the single
        aggregate file (cpu_<stack>_<tag>.txt, F2-re-measurement format)."""
        vals = []
        for cf in sorted(glob.glob(str(root / f"cpu_{stack}_{tag}_run[0-9].txt"))):
            m = re.search(r"_run(\d+)\.txt$", Path(cf).name)
            if not m:
                continue
            ticks = _parse_cpu(Path(cf))["sec"]
            reqs = _exact_req(root / f"wrk_{stack}_{tag}_{scenario}_run{m.group(1)}.txt")
            if ticks and reqs:
                vals.append(ticks / reqs * 1e6)
        if vals:
            vals.sort()
            return vals[len(vals) // 2]
        try:
            cpu = _parse_cpu(root / f"cpu_{stack}_{tag}.txt")["sec"]
        except (OSError, KeyError, ValueError):
            return None  # tag never measured for this stack (e.g. resphandler pre-F3)
        reqs = _scenario_req(stack, f"{tag}_{scenario}")
        return cpu / reqs * 1e6 if cpu and reqs else None

    def _parse_sum(path: Path, pat: str) -> float:
        """Sum field in ms from a scenario capture file (0 if absent)."""
        try:
            m = re.search(pat, path.read_text(errors="replace"))
            return float(m.group(1)) if m else 0.0
        except OSError:
            return 0.0

    def _b1_terms(stack: str):
        """Instrumented B1 quantities (same derivation as the F2 row)."""
        total_s = _parse_cpu(root / f"cpu_{stack}_B1_plaintext_c256.txt")["sec"]
        resp_ms = _parse_sum(root / f"resp_{stack}_B1_plaintext_c256.txt",
                             r"resp_sum_ms=([0-9.]+)")
        handler_ms = _parse_sum(root / f"handler_{stack}_B1_plaintext_c256.txt",
                                r"handler_sum_ms=([0-9.]+)")
        parse_ms = _parse_sum(root / f"parse_{stack}_B1_plaintext_c256.txt",
                              r"parse_sum_ms=([0-9.]+)")
        parse_calls = _parse_sum(root / f"parse_{stack}_B1_plaintext_c256.txt",
                                 r"parse_calls=([0-9.]+)")
        parse_scan_calls = _parse_sum(root / f"parse_{stack}_B1_plaintext_c256.txt",
                                      r"parse_scan_calls=([0-9.]+)")
        parse_scan_ms = _parse_sum(root / f"parse_{stack}_B1_plaintext_c256.txt",
                                   r"parse_scan_sum_ms=([0-9.]+)")
        parse_dispatch_ms = _parse_sum(root / f"parse_{stack}_B1_plaintext_c256.txt",
                                       r"parse_dispatch_sum_ms=([0-9.]+)")
        null_calls = _parse_sum(root / f"parse_{stack}_B1_plaintext_c256.txt",
                                r"null_calls=([0-9.]+)")
        null_ms = _parse_sum(root / f"parse_{stack}_B1_plaintext_c256.txt",
                             r"null_sum_ms=([0-9.]+)")
        reqs = _scenario_req(stack, "B1_plaintext_c256")
        if not total_s or not reqs:
            return None
        total_us = total_s / reqs * 1e6
        resp = resp_ms * 1000.0 / reqs
        handler = handler_ms * 1000.0 / reqs
        hlog = handler - resp if stack.startswith("blackbull") else handler
        parse = parse_ms * 1000.0 / reqs
        parse_scan = parse_scan_ms * 1000.0 / reqs
        parse_dispatch = parse_dispatch_ms * 1000.0 / reqs
        # Null-seam inflation I (inside-window clock share per bracket call).
        # 0 when the run did not arm BB_NULL_SEAM (backward compatible).
        I = null_ms * 1000.0 / null_calls if null_calls else 0.0
        # F4 app-dispatch seam (BB ``__call__`` / sanic ``handle_request``)
        # + async null I_a (both dispatch brackets are async).
        dispatch_ms = _parse_sum(root / f"parse_{stack}_B1_plaintext_c256.txt",
                                 r"dispatch_sum_ms=([0-9.]+)")
        dispatch_calls = _parse_sum(root / f"parse_{stack}_B1_plaintext_c256.txt",
                                    r"dispatch_calls=([0-9.]+)")
        null_a_calls = _parse_sum(root / f"parse_{stack}_B1_plaintext_c256.txt",
                                  r"null_a_calls=([0-9.]+)")
        null_a_ms = _parse_sum(root / f"parse_{stack}_B1_plaintext_c256.txt",
                               r"null_a_sum_ms=([0-9.]+)")
        dispatch = dispatch_ms * 1000.0 / reqs
        Ia = null_a_ms * 1000.0 / null_a_calls if null_a_calls else 0.0
        # F5 read-path seam (transport callbacks) + scan empty/data bucketing.
        gb_ms = _parse_sum(root / f"parse_{stack}_B1_plaintext_c256.txt",
                           r"get_buffer_sum_ms=([0-9.]+)")
        gb_calls = _parse_sum(root / f"parse_{stack}_B1_plaintext_c256.txt",
                              r"get_buffer_calls=([0-9.]+)")
        bu_ms = _parse_sum(root / f"parse_{stack}_B1_plaintext_c256.txt",
                           r"buffer_updated_sum_ms=([0-9.]+)")
        bu_calls = _parse_sum(root / f"parse_{stack}_B1_plaintext_c256.txt",
                              r"buffer_updated_calls=([0-9.]+)")
        dr_ms = _parse_sum(root / f"parse_{stack}_B1_plaintext_c256.txt",
                           r"data_received_sum_ms=([0-9.]+)")
        dr_calls = _parse_sum(root / f"parse_{stack}_B1_plaintext_c256.txt",
                              r"data_received_calls=([0-9.]+)")
        se_calls = _parse_sum(root / f"parse_{stack}_B1_plaintext_c256.txt",
                              r"scan_empty_calls=([0-9.]+)")
        se_ms = _parse_sum(root / f"parse_{stack}_B1_plaintext_c256.txt",
                           r"scan_empty_sum_ms=([0-9.]+)")
        return {"total": total_us, "resp": resp, "handler": handler,
                "hlog": hlog, "parse": parse, "parse_scan": parse_scan,
                "parse_scan_sum_ms": parse_scan_ms,
                "parse_dispatch": parse_dispatch, "reqs": reqs,
                "parse_calls": int(parse_calls), "parse_scan_calls": int(parse_scan_calls),
                "null_calls": int(null_calls), "null_ms": null_ms, "I": I,
                "dispatch": dispatch, "dispatch_calls": int(dispatch_calls),
                "null_a_calls": int(null_a_calls), "null_a_ms": null_a_ms, "Ia": Ia,
                "gb_calls": int(gb_calls), "gb_ms": gb_ms,
                "bu_calls": int(bu_calls), "bu_ms": bu_ms,
                "dr_calls": int(dr_calls), "dr_ms": dr_ms,
                "se_calls": int(se_calls), "se_ms": se_ms}

    # Calibration present: per-run cpu_*_bare_runN.txt (post-F3) or the single
    # cpu_*_bare.txt (F2-re-measurement format).
    cal_files = sorted(glob.glob(str(root / "cpu_*_bare_run[0-9].txt")))
    if not cal_files:
        cal_files = sorted(glob.glob(str(root / "cpu_*_bare.txt")))
    if not cal_files:
        print("| _(no cpu_<stack>_bare files — CALIBRATE not enabled)_ |")
        return

    stacks = []
    for f in cal_files:
        m = re.match(r"cpu_(.+)_bare(?:_run\d+)?\.txt$", Path(f).name)
        if m and m.group(1) not in stacks:
            stacks.append(m.group(1))

    # Honesty check: if the responly arm had the handler bracket armed
    # (handler_calls > 0 in its writer output), the per-seam split is void —
    # responly was instrumentally identical to full (F2-re-measurement bug).
    per_seam_void = False
    for stack in stacks:
        p = root / f"resp_{stack}_responly.txt"
        if p.exists():
            m = re.search(r"handler_calls=(\d+)", p.read_text(errors="replace"))
            if m and int(m.group(1)) > 0:
                per_seam_void = True
    if per_seam_void:
        print("| _(WARNING: responly arm shows handler_calls>0 — bracket leaked in; "
              "per-seam split VOID; per-arm diffs are run-to-run noise ~0.7 µs/req)_ |")

    rows = []
    for stack in sorted(stacks):
        t = _b1_terms(stack)
        full_us = t["total"] if t else None
        bare_us = _cal_us(stack, "bare", "B1_plaintext_c256")
        responly_us = _cal_us(stack, "responly", "B1_plaintext_c256")
        resphandler_us = _cal_us(stack, "resphandler", "B1_plaintext_c256")
        if full_us is None or bare_us is None or responly_us is None:
            print(f"| {stack} | _(no wrk logs for calibration)_ |")
            continue
        # resphandler missing (pre-F3 scratch): fall back to full so that
        # handler-bracket = full − responly and parse-seam = 0.
        if resphandler_us is None:
            resphandler_us = full_us
        resp_cost = responly_us - bare_us
        handler_cost = resphandler_us - responly_us
        parse_cost = full_us - resphandler_us
        total_cost = full_us - bare_us
        rows.append((stack, full_us, bare_us, responly_us, resphandler_us,
                     resp_cost, handler_cost, parse_cost, total_cost))
        print(f"| {stack} | {full_us:7.2f} | {bare_us:7.2f} | {responly_us:8.2f} "
              f"| {resphandler_us:11.2f} | {resp_cost:9.2f} "
              f"| {handler_cost:15.2f} | {parse_cost:10.2f} "
              f"| {total_cost:11.2f} |")

    if not rows:
        return

    # Gate stamps (F3+ review fix): every launch writes what it armed, so the
    # bare arm can prove itself (bare must show 0/0/0 — an inverted arm like
    # the run-2 bare anomaly is then caught structurally instead of recorded
    # as a caveat).
    def _gate(stack: str, tag: str) -> str:
        p = root / f"gate_{stack}_{tag}.txt" if tag else root / f"gate_{stack}.txt"
        try:
            return p.read_text(errors="replace").strip()
        except OSError:
            return "(no stamp)"

    gate_rows = [(s, _gate(s, ""), _gate(s, "bare"),
                  _gate(s, "responly"), _gate(s, "resphandler"))
                 for s in sorted(stacks)]
    if any(g != "(no stamp)" for _, *gs in gate_rows for g in gs):
        print()
        print("Gate stamps (resp/handler/parse armed per launch):")
        print("| stack | full | bare | responly | resphandler |")
        print("|---|---|---|---|---|")
        for s, full, bare, responly, resphandler in gate_rows:
            print(f"| {s} | {full} | {bare} | {responly} | {resphandler} |")

    # Corrected B1 verdict: subtract the instrument from total (front absorbs
    # it); resp/handler are I-corrected for the inside-window clock share (each
    # measured segment carries I per bracket — the null seam measures it).  BB's
    # hlog = handler − resp cancels its two I's; sanic's hlog = handler keeps
    # one.  front = total_c − resp_c − hlog_c is therefore consistently
    # corrected, and resp_c + hlog_c + front_c == total_c closes exactly.
    print()
    print("B1 corrected — instrument + null-seam (I) subtracted (CPU-µs/req):")
    print("| stack | total | resp | handler_logic | front |")
    print("|---|---:|---:|---:|---:|")
    corr = {}
    for row in rows:
        stack = row[0]
        total_cost = row[-1]
        t = _b1_terms(stack)
        if not t:
            print(f"| {stack} | _(missing B1 terms)_ |")
            continue
        I = t["I"]
        total_c = t["total"] - total_cost
        resp_c = t["resp"] - I
        hlog_c = t["hlog"] - (I if stack.startswith("sanic") else 0.0)
        front_c = total_c - resp_c - hlog_c
        corr[stack] = (total_c, resp_c, hlog_c, front_c)
        print(f"| {stack} | {total_c:7.2f} | {resp_c:7.2f} "
              f"| {hlog_c:13.2f} | {front_c:7.2f} |")

    if per_seam_void:
        print()
        print("  (calibration arms were identically instrumented — per-arm diffs are "
              "run-to-run noise; report front ≈ +6.8 ±0.7, not the corrected decimals)")

    def _primary(stack_names, prefix):
        """The comparison stack for *prefix*: prefer the -cleartext variant when
        a multi-transport run measured both (the sprint's B1 headline is
        cleartext), else the first sorted match, else the first of any kind."""
        names = sorted(stack_names)
        for n in names:
            if n.startswith(prefix) and n.endswith("-cleartext"):
                return n
        for n in names:
            if n.startswith(prefix) and "-noevents" not in n:
                return n
        for n in names:
            if n.startswith(prefix):
                return n
        return None

    bb_name = _primary(corr, "blackbull")
    sa_name = _primary(corr, "sanic")
    if bb_name and sa_name:
        bb, sa = corr[bb_name], corr[sa_name]
        print()
        print("Corrected B1 delta BB−sanic (µs/req):")
        print(f"  total  = {bb[0] - sa[0]:+.2f}")
        print(f"  resp   = {bb[1] - sa[1]:+.2f}")
        print(f"  handler_logic = {bb[2] - sa[2]:+.2f}")
        print(f"  front  = {bb[3] - sa[3]:+.2f}")

    # F3 parse seam — scope-corrected (F3 review) with the NULL-SEAM
    # correction (2nd F3 review): BB's parse includes the sync head scan;
    # sanic's includes its scan + Request construction (dispatches stripped by
    # TouchUp).  Each measured segment is inflated by the wrapper's
    # inside-window clock share I (one thread_time read + call setup per
    # bracket, by construction).  The null seam measures I directly (a noop
    # with the identical wrapper); the correction subtracts I × (bracket call
    # count) from each parse segment.  dispatch uses the CORRECTED (bare-
    # based) front — the uncorrected total carries the asymmetric instrument
    # cost (Phase B: BB +6.8 µs from the extra scan bracket).
    if any((root / f"parse_{s}_B1_plaintext_c256.txt").exists()
           for s, *_ in rows):
        print()
        print("F3 parse seam — scope + null-seam corrected (µs/req):")
        print("| stack | parse (measured) | I (null) | parse (corrected) | dispatch+machinery |")
        print("|---|---:|---:|---:|---:|")
        bb_pc = sa_pc = bb_disp = sa_disp = None
        bb_resp = sa_resp = bb_hlog = sa_hlog = None
        bb_total = sa_total = None
        # Prefer the cleartext pair when a multi-transport run measured both.
        bb_primary = _primary(corr, "blackbull")
        sa_primary = _primary(corr, "sanic")
        for stack, *_ in rows:
            t = _b1_terms(stack)
            if not t or stack not in corr:
                continue
            if not (root / f"parse_{stack}_B1_plaintext_c256.txt").exists():
                print(f"| {stack} | _(seam not armed)_ | | | |")
                continue
            # Consistent front (I-corrected resp/hlog from the corrected-B1
            # section): front_c = total_c − resp_c − hlog_c.
            front_c = corr[stack][3]
            parse_us = t["parse"]
            pd_us = t["parse_dispatch"] if stack.startswith("sanic") else 0.0
            pc_us = parse_us - pd_us
            # null-seam: I = inside-window inflation per bracket call
            n_calls = t["null_calls"]
            I = t["null_ms"] * 1000.0 / n_calls if n_calls else 0.0
            br_calls = t["parse_calls"]
            if stack.startswith("blackbull"):
                br_calls += t["parse_scan_calls"]
            pc_corr = pc_us - br_calls * I / t["reqs"]
            disp_us = front_c - pc_corr
            if stack.startswith("blackbull") and bb_pc is None and stack == bb_primary:
                bb_pc, bb_disp = pc_corr, disp_us
                bb_resp, bb_hlog = corr[stack][1], corr[stack][2]
                bb_total = corr[stack][0]
            if stack.startswith("sanic") and sa_pc is None and stack == sa_primary:
                sa_pc, sa_disp = pc_corr, disp_us
                sa_resp, sa_hlog = corr[stack][1], corr[stack][2]
                sa_total = corr[stack][0]
            print(f"| {stack} | {pc_us:15.2f} | {I:6.3f} | {pc_corr:17.2f} | {disp_us:17.2f} |")
        if bb_pc is not None and sa_pc is not None:
            print()
            print(f"  parse_construct delta (corrected) BB−sanic = {bb_pc - sa_pc:+.2f} µs/req")
            print(f"  dispatch+machinery delta (corrected front) BB−sanic = {bb_disp - sa_disp:+.2f} µs/req")
            # Closure (3rd-review fix): the fully I-corrected decomposition must
            # sum to the instrument-free bare-total delta.  resp_c + hlog_c +
            # parse_c + dispatch_c == bare-total delta per pair; gap > 0.3 µs
            # means a segment's I (or scope) is inconsistent.
            resp_d, hlog_d = bb_resp - sa_resp, bb_hlog - sa_hlog
            pc_d, disp_d = bb_pc - sa_pc, bb_disp - sa_disp
            bare_d = bb_total - sa_total
            closed = resp_d + hlog_d + pc_d + disp_d
            gap = closed - bare_d
            flag = "OK" if abs(gap) <= 0.3 else "⚠ CLOSURE FAIL"
            print()
            print("  closure: resp + hlog + parse + dispatch (I-corrected) vs bare-total delta:")
            print(f"    resp {resp_d:+.2f} + hlog {hlog_d:+.2f} + parse {pc_d:+.2f} + "
                  f"dispatch {disp_d:+.2f} = {closed:+.2f}")
            print(f"    bare-total delta = {bare_d:+.2f}   gap = {gap:+.3f}  [{flag}]")
            print(f"  response-side net (resp+hlog) = {resp_d + hlog_d:+.2f} µs/req")

    # F4 app-dispatch seam — BB ``BlackBull.__call__`` / sanic
    # ``Sanic.handle_request`` (one async bracket per request, sequential after
    # the parse seam, containing router lookup + handler + send).  The fork
    # splits the F3 "dispatch+machinery" residual into:
    #   app_dispatch = the measured dispatch bracket (I_a-corrected; both
    #                  stacks async → I_a symmetric, so the DELTA is I-free)
    #   machinery    = bare − parse − app_dispatch  (everything outside the
    #                  app call: access-log prep, sender reset, recipient
    #                  bind, the read/wait infrastructure, keep-alive loop…)
    # IMPORTANT framing: app_dispatch ⊇ hlog + resp (the dispatch bracket
    # wraps the handler and send seams), so machinery MUST be computed from
    # the bare total (total_c), NOT from the front (front already subtracted
    # resp + hlog, which are inside app_dispatch — front-based machinery
    # double-subtracts and goes negative).  Closure: parse + app_dispatch +
    # machinery == total_c (bare) by construction.
    # NESTING CORRECTION (4th-review fix): app_dispatch also contains the
    # INNER resp + handler brackets' wrapper cost (the handler and send run
    # inside __call__/handle_request).  Subtract C_resp + C_hand (the
    # calibration ladder's resp-seam + handler-bracket costs) from
    # app_dispatch to get the true outer-region cost; machinery =
    # bare − parse − app_dispatch_nested.  The reported "glue"
    # (app_dispatch − hlog − resp) is NOT dispatch plumbing — it is the
    # inner brackets' cost and goes negative for BB (proving the artifact);
    # it is reported only as a diagnostic, never as a headline term.
    if any((root / f"parse_{s}_B1_plaintext_c256.txt").exists()
           for s, *_ in rows):
        have_dispatch = any(
            _b1_terms(s) and _b1_terms(s)["dispatch_calls"] > 0
            for s, *_ in rows)
        if have_dispatch:
            print()
            print("F4 app-dispatch seam (µs/req; nesting-corrected):")
            print("| stack | dispatch (measured) | I_a | app_dispatch (I_a-corr) | app_dispatch (nest-corr) | machinery (nest-corr) |")
            print("|---|---:|---:|---:|---:|---:|")
            bb_dc = sa_dc = bb_mc = sa_mc = None
            bb_ad_meas = sa_ad_meas = None
            bb_primary = _primary(corr, "blackbull")
            sa_primary = _primary(corr, "sanic")
            for row in rows:
                stack = row[0]
                c_resp = row[5]   # resp-seam cost (responly − bare)
                c_hand = row[6]   # handler-bracket cost (resphandler − responly)
                t = _b1_terms(stack)
                if not t or stack not in corr:
                    continue
                total_c = corr[stack][0]  # bare total (instrument-free)
                pc = t["parse"]
                if stack.startswith("blackbull"):
                    pd_us = 0.0
                else:
                    pd_us = t["parse_dispatch"]
                pc_us = pc - pd_us
                I = t["I"]
                br_calls = t["parse_calls"]
                if stack.startswith("blackbull"):
                    br_calls += t["parse_scan_calls"]
                pc_corr = pc_us - br_calls * I / t["reqs"]
                Ia = t["Ia"]
                ad_corr = t["dispatch"] - Ia          # I_a-corrected only
                ad_nest = ad_corr - (c_resp + c_hand)  # nesting-corrected
                mach = total_c - pc_corr - ad_nest
                if stack.startswith("blackbull") and bb_dc is None and stack == bb_primary:
                    bb_dc, bb_mc = ad_nest, mach
                    bb_ad_meas = ad_corr
                if stack.startswith("sanic") and sa_dc is None and stack == sa_primary:
                    sa_dc, sa_mc = ad_nest, mach
                    sa_ad_meas = ad_corr
                print(f"| {stack} | {t['dispatch']:15.2f} | {Ia:6.3f} | "
                      f"{ad_corr:16.2f} | {ad_nest:17.2f} | {mach:20.2f} |")
            if bb_dc is not None and sa_dc is not None:
                print()
                print(f"  app_dispatch delta (nesting-corrected) BB−sanic = {bb_dc - sa_dc:+.2f} µs/req")
                print(f"  machinery delta (bare residual, nesting-corrected) BB−sanic = {bb_mc - sa_mc:+.2f} µs/req")
                # sanity: the MEASURED (I_a-corrected, pre-nesting) app_dispatch
                # must contain hlog + resp (the bracket wraps the handler/send);
                # the nesting-corrected value is only the outer-region cost.
                bb_ar = corr[bb_name][1] + corr[bb_name][2] if bb_name else None
                if bb_ar is not None and bb_ad_meas is not None:
                    note = ("measured app_dispatch ⊇ hlog+resp" if bb_ad_meas >= bb_ar
                            else "⚠ app_dispatch < hlog+resp (scope overlap violated)")
                    print(f"  BB check: measured app_dispatch {bb_ad_meas:.2f} vs hlog+resp {bb_ar:.2f} — {note}")
                print("  (the inner 'dispatch-glue' term is an instrument artifact — the outer bracket "
                      "nests the handler/resp seams; negative glue for BB proves it.  Do not quote it "
                      "as a term.  machinery is the nesting-corrected residual and carries the "
                      "read/wait infrastructure outside the parse seam.)")

    # F5 read-path seam — the transport read callbacks.  BB is a
    # BufferedProtocol → TWO sync callbacks per read (get_buffer +
    # buffer_updated, each creating/dropping a memoryview); sanic is a plain
    # asyncio.Protocol → ONE sync callback (data_received, no memoryview).
    # All sync (I_sync correction per call, no parking).  Per-request cost =
    # (sum of callback dts − brackets×I) / reqs.  This is the transport-shape
    # term the review hypothesises inside machinery (costs in cleartext,
    # pays back ~3.2 µs/req under TLS per F3b).  Also prints the head-scan
    # empty/data bucketing (the empty-buffer first call ≈ free; the data
    # call is the real scan).
    if any((root / f"parse_{s}_B1_plaintext_c256.txt").exists()
           for s, *_ in rows):
        have_read = any(
            _b1_terms(s) and _b1_terms(s)["gb_calls"] + _b1_terms(s)["dr_calls"] > 0
            for s, *_ in rows)
        if have_read:
            print()
            print("F5 read-path seam (transport callbacks, I-corrected, µs/req):")
            print("| stack | get_buffer calls/req | buffer_updated calls/req | data_received calls/req | read-path (net) |")
            print("|---|---:|---:|---:|---:|")
            read_us = {}
            for stack, *_ in rows:
                t = _b1_terms(stack)
                if not t:
                    continue
                req = t["reqs"]
                I = t["I"]
                gb_c, bu_c, dr_c = t["gb_calls"], t["bu_calls"], t["dr_calls"]
                r = 0.0
                if stack.startswith("blackbull"):
                    r = ((t["gb_ms"] + t["bu_ms"]) * 1000.0
                         - (gb_c + bu_c) * I) / req
                else:
                    r = (t["dr_ms"] * 1000.0 - dr_c * I) / req
                read_us[stack] = r
                print(f"| {stack} | {gb_c / req:20.2f} | {bu_c / req:22.2f} | "
                      f"{dr_c / req:21.2f} | {r:11.2f} |")
            # Per-transport delta (the term has OPPOSITE signs in cleartext vs
            # TLS — F3b found TLS selects _do_read__buffered for a
            # BufferedProtocol, so BB pays the 2-callback+memoryview cost in
            # cleartext but not under TLS).  Report each transport separately.
            print()
            for transport, suffix in (("cleartext", "-cleartext"), ("TLS", "")):
                bb_key = next((s for s in read_us if s == f"blackbull{suffix}"), None)
                sa_key = next((s for s in read_us if s == f"sanic{suffix}"), None)
                if bb_key and sa_key:
                    d = read_us[bb_key] - read_us[sa_key]
                    print(f"  read-path delta ({transport}) BB−sanic = {d:+.2f} µs/req")
            if read_us:
                print("  (per-transport deltas above; the single 'read-path delta' is NOT a valid"
                      "merge — cleartext and TLS have opposite signs for this term)")
            # head-scan bucketing (empty vs data)
            print()
            print("  head-scan bucketing (µs/call, I-corrected):")
            for stack, *_ in rows:
                t = _b1_terms(stack)
                if not t or t["se_calls"] == 0:
                    continue
                req = t["reqs"]
                I = t["I"]
                empty_true = t["se_ms"] * 1000.0 / t["se_calls"] - I
                data_calls = t["parse_scan_calls"] - t["se_calls"]
                data_true = ((t["parse_scan_sum_ms"] - t["se_ms"]) * 1000.0 / data_calls - I
                             if data_calls else 0.0)
                empty_share = t["se_calls"] / t["parse_scan_calls"]
                print(f"    {stack}: empty {t['se_calls'] / req:.2f}/req ({empty_share * 100:.0f}% of calls, "
                      f"true {empty_true:.3f} µs)  data {data_calls / req:.2f}/req "
                      f"(true {data_true:.3f} µs)  empty/req {empty_true * t['se_calls'] / req:.3f} µs")


def _parse_cpu(path: Path) -> dict:
    txt = path.read_text(errors="replace")
    m = re.search(r"ticks=(\d+)\s+clk_tck=(\d+)", txt)
    if not m:
        return {"sec": 0.0}
    return {"sec": int(m.group(1)) / max(int(m.group(2)), 1)}


def _parse_resp(path: Path) -> dict:
    txt = path.read_text(errors="replace")
    m = re.search(r"sum_ms=([0-9.]+)", txt)
    return {"sum_us": float(m.group(1)) * 1000.0 if m else 0.0}


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(f"Usage: {sys.argv[0]} <scratch-or-results-dir>", file=sys.stderr)
        sys.exit(2)
    main(sys.argv[1])
