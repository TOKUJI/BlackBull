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
"""
import glob
import json
import os
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
    table = render(rows, frameworks)
    out = os.path.join(result_dir, "COMPARISON.md")
    with open(out, "w") as fh:
        fh.write(table)
    print(table)
    print(f"[compare_table] wrote {out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
