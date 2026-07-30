#!/usr/bin/env python3
"""Emit a simple per-profile framework comparison table for an HttpArena run.

Reads the result JSONs an ``httparena_compare.sh`` run pulled back to
``<result_dir>/httparena-tree/results/<profile>/<conns>/<framework>.json`` and
writes a markdown table (``COMPARISON.md``) into the result dir, also echoing it
to stdout.

Usage:
    python3 compare_table.py <result_dir>

Stdlib only, no third-party deps — it runs on the bench driver host at the end
of a successful benchmark.  When ``blackbull`` and exactly one peer framework
are present, a ``BB/peer`` throughput ratio column is added; otherwise it just
lists each framework's req/s side by side.

The JSON's ``rps`` is HttpArena's **best of three** runs.  Best-of-N is a fine
headline but a poor basis for judging a small gap: it reports no spread, so a
3 % difference between two arms is indistinguishable from one arm getting a
luckier run.  So a second table re-reads every individual run out of
``logs/benchmark-<framework>-<profile>.log`` and prints mean ± spread beside
the best.  Read a gap as real only when it is large against the spread column.
"""
import glob
import json
import os
import re
import sys

# Canonical profile order (matches the suite's default PROFILES); anything not
# listed sorts after these, alphabetically, so unknown/new profiles still show.
_ORDER = [
    "baseline", "json", "json-tls", "static", "baseline-h2", "static-h2",
    "echo-ws", "echo-ws-pipeline", "pipelined", "limited-conn", "json-comp",
    "upload", "crud", "async-db", "api-4", "api-16", "fortunes", "gateway",
    "unary-grpc", "unary-grpc-tls", "stream-grpc", "stream-grpc-tls",
]


def _profile_key(profile: str, conns: int) -> tuple:
    idx = _ORDER.index(profile) if profile in _ORDER else len(_ORDER)
    return (idx, profile, conns)


def collect(result_dir: str) -> tuple[dict, list]:
    """Return ({(profile, conns): {fw: rps}}, [frameworks_seen])."""
    base = os.path.join(result_dir, "httparena-tree", "results")
    rows: dict = {}
    frameworks: list = []
    for path in glob.glob(os.path.join(base, "*", "*", "*.json")):
        parts = path.split(os.sep)
        profile, conns, fw = parts[-3], parts[-2], parts[-1][:-5]
        try:
            with open(path) as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            continue
        rps = data.get("rps")
        if rps is None:
            continue
        try:
            conns_i = int(conns)
        except ValueError:
            continue
        rows.setdefault((profile, conns_i), {})[fw] = rps
        if fw not in frameworks:
            frameworks.append(fw)
    # blackbull first, then the rest alphabetically — stable, readable columns.
    frameworks.sort(key=lambda f: (f != "blackbull", f))
    return rows, frameworks


# "=== blackbull / baseline / 512c (tool=gcannon) ===" — the section header
# benchmark.sh prints before each (framework, profile, conns) group of runs.
_HEAD_RE = re.compile(r'^===\s*(\S+)\s*/\s*(\S+)\s*/\s*(\d+)c\b')
# "  Throughput: 179.61K req/s" — one per run within the group.
_RUN_RE = re.compile(r'Throughput:\s*([0-9.]+)\s*([KM]?)\s*req/s')

_SUFFIX = {'': 1.0, 'K': 1e3, 'M': 1e6}


def collect_runs(result_dir: str) -> dict:
    """Return {(profile, conns): {fw: [rps, ...]}} from the benchmark logs.

    The per-run throughputs never reach the JSON — only their maximum does —
    so the logs are the only place the spread survives.  benchmark.sh prints
    them rounded to ~4 significant figures ("179.61K req/s"), which quantises
    these figures by ~0.05 % — two orders below the spread they are read for.
    """
    runs: dict = {}
    pattern = os.path.join(result_dir, "logs", "benchmark-*.log")
    for path in glob.glob(pattern):
        key = None
        with open(path, errors="replace") as fh:
            for line in fh:
                head = _HEAD_RE.match(line.strip())
                if head:
                    fw, profile, conns = head.group(1), head.group(2), int(head.group(3))
                    key = (profile, conns, fw)
                    continue
                run = _RUN_RE.search(line)
                if run and key is not None:
                    value = float(run.group(1)) * _SUFFIX[run.group(2)]
                    profile, conns, fw = key
                    runs.setdefault((profile, conns), {}).setdefault(fw, []).append(value)
    return runs


def render_runs(runs: dict, frameworks: list) -> str:
    """Mean ± spread beside the best, so a small gap can be judged."""
    if not runs:
        return ""
    lines = [
        "",
        "## Per-run throughput (mean of all runs, spread = (max-min)/mean)",
        "",
        "Best-of-3 is what the JSON records; this is every run.  A gap between "
        "two arms is a finding only when it is large next to the spread.",
        "",
        "| profile/conns | framework | runs | mean req/s | best req/s | spread |",
        "|---|---|---|---:|---:|---:|",
    ]
    for key in sorted(runs, key=lambda k: _profile_key(*k)):
        profile, conns = key
        for fw in frameworks:
            values = runs[key].get(fw)
            if not values:
                continue
            mean = sum(values) / len(values)
            spread = (max(values) - min(values)) / mean if mean else 0.0
            lines.append(
                f"| {profile}/{conns} | {fw} | {len(values)} | "
                f"{mean:,.0f} | {max(values):,.0f} | {spread * 100:.1f}% |"
            )
    return "\n".join(lines) + "\n"


def render_uvloop_delta(runs: dict) -> str:
    """What uvloop is worth, when both loop arms ran in this session.

    This is the only honest form of the question.  Comparing a uvloop number
    from one run against a stock number from another confounds the loop with
    the machine, the generator, the peer set, and the build; here the two arms
    are the same wheel and the same app on one instance, differing in one ENV.

    The verdict column compares the delta against the larger of the two arms'
    own run-to-run spreads: a delta inside the noise is not a measurement.
    """
    stock_key, uv_key = "blackbull", "blackbull-uvloop"
    present = {fw for cell in runs.values() for fw in cell}
    if not {stock_key, uv_key} <= present:
        return ""

    lines = [
        "",
        "## uvloop delta (same instance, same session, same wheel)",
        "",
        "| profile/conns | stock mean | uvloop mean | Δ | max spread | verdict |",
        "|---|---:|---:|---:|---:|---|",
    ]
    for key in sorted(runs, key=lambda k: _profile_key(*k)):
        stock, uvloop = runs[key].get(stock_key), runs[key].get(uv_key)
        if not stock or not uvloop:
            continue
        s_mean, u_mean = sum(stock) / len(stock), sum(uvloop) / len(uvloop)
        delta = (u_mean - s_mean) / s_mean
        spread = max((max(v) - min(v)) / (sum(v) / len(v)) for v in (stock, uvloop))
        verdict = "inside noise" if abs(delta) <= spread else "outside noise"
        profile, conns = key
        lines.append(
            f"| {profile}/{conns} | {s_mean:,.0f} | {u_mean:,.0f} | "
            f"{delta * 100:+.1f}% | {spread * 100:.1f}% | {verdict} |"
        )
    return "\n".join(lines) + "\n"


def render(rows: dict, frameworks: list) -> str:
    ratio_peer = None
    if "blackbull" in frameworks and len(frameworks) == 2:
        ratio_peer = next(f for f in frameworks if f != "blackbull")

    header = ["profile/conns"] + [f"{f} req/s" for f in frameworks]
    if ratio_peer:
        header.append(f"BB/{ratio_peer}")
    lines = ["| " + " | ".join(header) + " |",
             "|" + "|".join(["---"] * len(header)) + "|"]

    for key in sorted(rows, key=lambda k: _profile_key(*k)):
        profile, conns = key
        cells = [f"{profile}/{conns}"]
        vals = rows[key]
        for f in frameworks:
            v = vals.get(f)
            cells.append(f"{v:,}" if v is not None else "—")
        if ratio_peer:
            bb, pv = vals.get("blackbull"), vals.get(ratio_peer)
            cells.append(f"{bb / pv:.2f}x" if (bb and pv) else "—")
        lines.append("| " + " | ".join(cells) + " |")

    return "# Framework comparison — req/s per profile\n\n" + "\n".join(lines) + "\n"


def main(argv: list) -> int:
    if len(argv) != 2:
        print(f"usage: {argv[0]} <result_dir>", file=sys.stderr)
        return 2
    result_dir = argv[1]
    rows, frameworks = collect(result_dir)
    if not rows:
        print(f"compare_table.py: no result JSONs under {result_dir}", file=sys.stderr)
        return 1
    runs = collect_runs(result_dir)
    table = (render(rows, frameworks)
             + render_runs(runs, frameworks)
             + render_uvloop_delta(runs))
    out = os.path.join(result_dir, "COMPARISON.md")
    with open(out, "w") as fh:
        fh.write(table)
    print(table)
    print(f"[compare_table] wrote {out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
