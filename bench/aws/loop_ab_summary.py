#!/usr/bin/env python3
"""Summarise a ``loop_ab_nginx.sh`` run into a decision-quality table.

Reads the raw wrk output ``loop_ab_nginx.sh`` pulled back to
``<result_dir>/raw/wrk-<arm>-<profile>-c<cycle>.txt`` and writes
``RESULT.md`` into the result dir, echoing it to stdout.

Usage:
    python3 loop_ab_summary.py <result_dir> [--instance TYPE] [--workers N]
                                            [--conns N]

Stdlib only — it runs on the bench driver host at the end of a run.

Every arm is measured $CYCLES times, interleaved with the other arm, so
each row carries a spread alongside its mean.  The uvloop delta is a
finding only when it is large next to that spread; a delta inside the
spread is reported as such rather than quoted as a number.
"""
import argparse
import glob
import os
import re
import statistics
import sys

# "Requests/sec:  40723.94" — wrk's throughput line.
_RPS_RE = re.compile(r'^Requests/sec:\s*([0-9.]+)', re.M)
# "  99%   12.34ms" from wrk --latency's distribution block.
_P99_RE = re.compile(r'^\s*99%\s+([0-9.]+)(us|ms|s)\s*$', re.M)
# The footer loop_ab_nginx.sh appends: "reconnects=41 requests=1222899".
_CONN_RE = re.compile(r'^reconnects=(-?\d+)\s+requests=(\d+)', re.M)
# wrk-A-ping-c1.txt
_NAME_RE = re.compile(r'^wrk-(\w+)-([\w.-]+)-c(\d+)\.txt$')

_UNIT_MS = {'us': 1e-3, 'ms': 1.0, 's': 1e3}

# Arm A is uvloop, arm B is stock asyncio — set by loop_ab_nginx.sh.
UVLOOP_ARM, STOCK_ARM = 'A', 'B'
_ARM_LABEL = {UVLOOP_ARM: 'A: CPython+uvloop', STOCK_ARM: 'B: CPython+asyncio'}


def collect(result_dir):
    """Return {profile: {arm: [{'rps', 'p99_ms', 'reconnects_per_req'}, ...]}}."""
    runs = {}
    for path in sorted(glob.glob(os.path.join(result_dir, "raw", "wrk-*.txt"))):
        name = _NAME_RE.match(os.path.basename(path))
        if not name:
            continue
        arm, profile, cycle = name.group(1), name.group(2), int(name.group(3))
        with open(path, errors="replace") as fh:
            text = fh.read()
        rps = _RPS_RE.search(text)
        if not rps:
            print(f"loop_ab_summary: no Requests/sec in {path} — skipped",
                  file=sys.stderr)
            continue
        p99 = _P99_RE.search(text)
        conn = _CONN_RE.search(text)
        runs.setdefault(profile, {}).setdefault(arm, []).append({
            'cycle': cycle,
            'rps': float(rps.group(1)),
            'p99_ms': float(p99.group(1)) * _UNIT_MS[p99.group(2)] if p99 else None,
            'reconnects_per_req': (int(conn.group(1)) / int(conn.group(2))
                                   if conn and int(conn.group(2)) else None),
        })
    return runs


def _spread(values):
    mean = statistics.fmean(values)
    return (max(values) - min(values)) / mean if mean else 0.0


def render(runs, meta):
    lines = [
        "# What uvloop is worth on CPython, behind nginx",
        "",
        f"**Instance**: {meta.instance} (single host: wrk + nginx + BlackBull)  ",
        f"**Topology**: `wrk -c{meta.conns} -> nginx :8443 (TLS) -> "
        f"BlackBull :8444 (cleartext H1, keep-alive pooled)`  ",
        f"**Per arm**: {meta.workers} workers; arms interleaved across cycles.",
        "",
        "Arm A is `BB_UVLOOP=1`, arm B is `BB_UVLOOP=0`; same wheel, same app,",
        "same nginx.  Every figure below comes from this one instance in this",
        "one session — nothing here is comparable to a number from another run.",
        "",
        "## Per-arm throughput",
        "",
        "| profile | arm | runs | mean req/s | min | max | spread | p99 (ms) |",
        "|---|---|---:|---:|---:|---:|---:|---:|",
    ]
    for profile in sorted(runs):
        for arm in (UVLOOP_ARM, STOCK_ARM):
            entries = runs[profile].get(arm)
            if not entries:
                continue
            rps = [e['rps'] for e in entries]
            p99 = [e['p99_ms'] for e in entries if e['p99_ms'] is not None]
            lines.append(
                f"| /{profile} | {_ARM_LABEL[arm]} | {len(rps)} | "
                f"{statistics.fmean(rps):,.0f} | {min(rps):,.0f} | "
                f"{max(rps):,.0f} | {_spread(rps) * 100:.1f}% | "
                f"{statistics.fmean(p99):.2f} |" if p99 else
                f"| /{profile} | {_ARM_LABEL[arm]} | {len(rps)} | "
                f"{statistics.fmean(rps):,.0f} | {min(rps):,.0f} | "
                f"{max(rps):,.0f} | {_spread(rps) * 100:.1f}% | — |"
            )

    lines += [
        "",
        "## A -> B: what uvloop is worth",
        "",
        "| profile | uvloop mean | asyncio mean | uvloop's worth | max spread | verdict |",
        "|---|---:|---:|---:|---:|---|",
    ]
    for profile in sorted(runs):
        uv = [e['rps'] for e in runs[profile].get(UVLOOP_ARM, [])]
        st = [e['rps'] for e in runs[profile].get(STOCK_ARM, [])]
        if not uv or not st:
            continue
        u_mean, s_mean = statistics.fmean(uv), statistics.fmean(st)
        delta = (u_mean - s_mean) / s_mean
        spread = max(_spread(uv), _spread(st))
        verdict = "inside noise" if abs(delta) <= spread else "outside noise"
        lines.append(
            f"| /{profile} | {u_mean:,.0f} | {s_mean:,.0f} | "
            f"{delta * 100:+.1f}% | {spread * 100:.1f}% | {verdict} |"
        )

    # A run where nginx failed to pool upstream connections measures TCP
    # handshakes, not request handling, so surface the ratio rather than
    # leaving it in the raw files for nobody to read.
    ratios = [e['reconnects_per_req']
              for prof in runs.values() for arm in prof.values()
              for e in arm if e['reconnects_per_req'] is not None]
    if ratios:
        worst = max(ratios)
        lines += [
            "",
            f"**Upstream keep-alive**: worst reconnects/request = {worst:.4f} "
            + ("— pooling held, the numbers measure request handling."
               if worst < 0.01 else
               "— **pooling did not hold; this run measures connection setup**."),
        ]

    lines += ["", "## Per-cycle detail", "",
              "| profile | arm | cycle | req/s |", "|---|---|---:|---:|"]
    for profile in sorted(runs):
        for arm in (UVLOOP_ARM, STOCK_ARM):
            for e in sorted(runs[profile].get(arm, []), key=lambda x: x['cycle']):
                lines.append(f"| /{profile} | {arm} | {e['cycle']} | {e['rps']:,.0f} |")

    return "\n".join(lines) + "\n"


def main(argv):
    ap = argparse.ArgumentParser()
    ap.add_argument("result_dir")
    ap.add_argument("--instance", default="unknown")
    ap.add_argument("--workers", default="?")
    ap.add_argument("--conns", default="?")
    meta = ap.parse_args(argv[1:])

    runs = collect(meta.result_dir)
    if not runs:
        print(f"loop_ab_summary: no wrk output under {meta.result_dir}/raw",
              file=sys.stderr)
        return 1
    table = render(runs, meta)
    out = os.path.join(meta.result_dir, "RESULT.md")
    with open(out, "w") as fh:
        fh.write(table)
    print(table)
    print(f"[loop_ab_summary] wrote {out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
