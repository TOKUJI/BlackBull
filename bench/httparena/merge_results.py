#!/usr/bin/env python3
"""Merge HttpArena results from multiple runs into a single side-by-side table.

Reads the raw result JSONs from each result directory:
    <result_dir>/httparena-tree/results/<profile>/<conns>/<framework>.json

Auto-labels each BlackBull column with its version (from provenance.md), and
each non-BlackBull framework by name.  Produces a merged markdown table with
per-column ratios against the newest BlackBull version.

Usage:
    python3 merge_results.py <dir1> <dir2> ... <dirN>  [--csv]

Examples:
    # After the 3-pass benchmark completes:
    python3 merge_results.py \\
        bench/results/httparena/compare-v0330-fastapi-20260724-*/ \\
        bench/results/httparena/compare-v0591-20260724-*/ \\
        bench/results/httparena/compare-v0610-20260724-*/

    # With explicit labels (overrides auto-detection):
    python3 merge_results.py --labels "v0.33.0,v0.59.1,v0.61.0" dir1/ dir2/ dir3/

    # Output as CSV instead of markdown:
    python3 merge_results.py --csv dir1/ dir2/ dir3/

Stdlib only — runs anywhere Python 3.11+ is available.
"""
import argparse
import glob
import json
import os
import re
import sys

# ── Canonical profile order (matches compare_table.py) ────────────────────
_ORDER = [
    "baseline", "json", "json-tls", "static", "baseline-h2", "static-h2",
    "echo-ws", "echo-ws-pipeline", "pipelined", "limited-conn", "json-comp",
    "upload", "crud", "async-db", "api-4", "api-16", "fortunes", "gateway",
    "unary-grpc", "unary-grpc-tls", "stream-grpc", "stream-grpc-tls",
]


def _profile_key(profile: str, conns: int) -> tuple:
    idx = _ORDER.index(profile) if profile in _ORDER else len(_ORDER)
    return (idx, profile, conns)


def _read_provenance(result_dir: str) -> dict:
    """Parse provenance.md into a dict of key-value pairs."""
    info: dict = {}
    path = os.path.join(result_dir, "provenance.md")
    if not os.path.isfile(path):
        return info
    with open(path) as fh:
        text = fh.read()

    # Extract "BlackBull:  blackbull==X.Y.Z (local wheel: blackbull-W.X.Y.Z-...)"
    # Prefer the wheel filename version (actual installed version) over the
    # pyproject.toml version (working-tree version, always HEAD).
    m = re.search(r'local wheel:\s+blackbull-(\d+\.\d+\.\d+)', text)
    if m:
        info["bb_version"] = m.group(1)
    else:
        # Fallback: the blackbull== version from the start of the line
        m = re.search(r'BlackBull:\s+blackbull==(\S+)', text)
        if m:
            info["bb_version"] = m.group(1)

    # Extract "Frameworks: ..." line
    m = re.search(r'Frameworks:\s+(.+)', text)
    if m:
        info["frameworks"] = [fw.strip() for fw in m.group(1).split()]

    # Extract "Sprint tag: ..." line
    m = re.search(r'Sprint tag:\s+(.+)', text)
    if m:
        info["sprint_tag"] = m.group(1).strip()

    return info


def _collect_from_dir(result_dir: str) -> tuple[dict, str | None]:
    """Collect (profile, conns) -> {column_label: rps} from a result dir.

    Returns (rows, bb_version_or_none) tuple.
    Labels are computed per-directory so that BlackBull from different
    directories gets different column labels (BB v0.33.0, BB v0.59.1, …).
    Non-BlackBull frameworks keep their bare name (e.g. "fastapi").
    """
    base = os.path.join(result_dir, "httparena-tree", "results")
    if not os.path.isdir(base):
        print(f"  [warn] no httparena-tree/results/ in {result_dir}", file=sys.stderr)
        return {}, None

    provenance = _read_provenance(result_dir)
    bb_version = provenance.get("bb_version")
    frameworks_in_dir = provenance.get("frameworks", [])

    # Build a *local* label map for this directory only.
    #   blackbull → "BB <version>"     (one per dir)
    #   anything else → bare name      (e.g. "fastapi")
    local_labels: dict = {}
    for fw in frameworks_in_dir:
        if fw == "blackbull" and bb_version:
            local_labels[fw] = f"BB {bb_version}"
        elif fw not in local_labels:
            local_labels[fw] = fw

    # Also handle the case where the JSON files exist but the framework
    # wasn't listed in provenance.md (defensive).
    def _resolve_label(fw: str) -> str:
        if fw in local_labels:
            return local_labels[fw]
        if fw == "blackbull":
            ver = bb_version or "?"
            lbl = f"BB {ver}"
            local_labels[fw] = lbl
            return lbl
        local_labels[fw] = fw
        return fw

    rows: dict = {}
    json_glob = os.path.join(base, "*", "*", "*.json")
    for path in sorted(glob.glob(json_glob)):
        parts = path.split(os.sep)
        profile, conns_str, fw_file = parts[-3], parts[-2], parts[-1]
        fw = fw_file[:-5]  # strip ".json"

        label = _resolve_label(fw)

        try:
            conns = int(conns_str)
        except ValueError:
            continue

        try:
            with open(path) as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            continue

        rps = data.get("rps")
        if rps is None:
            continue

        rows.setdefault((profile, conns), {})[label] = rps

    return rows, bb_version



def _merge_rows(all_rows: list[dict]) -> dict:
    """Merge multiple {(profile, conns): {label: rps}} dicts into one."""
    merged: dict = {}
    for rows in all_rows:
        for key, vals in rows.items():
            merged.setdefault(key, {}).update(vals)
    return merged


def _fmt_num(n: float | int) -> str:
    """Format a number with comma separators."""
    if isinstance(n, float):
        if n >= 1_000_000:
            return f"{n:,.0f}"
        elif n >= 100:
            return f"{n:,.0f}"
        else:
            return f"{n:,.1f}"
    return f"{n:,}"


def render_markdown(rows: dict, columns: list, ratio_base: str | None = None) -> str:
    """Render a merged markdown comparison table."""
    header = ["profile/conns"] + [f"{c} req/s" for c in columns]
    if ratio_base and ratio_base in columns:
        base_short = ratio_base.split()[-1]  # e.g. "v0.61.0" or "0.61.0"
        for c in columns:
            if c != ratio_base and c.startswith("BB "):
                col_short = c.split()[-1]
                # Ratio = newest / older (speedup factor: >1.0x = improved)
                header.append(f"{base_short}/{col_short}")
    lines = ["| " + " | ".join(header) + " |",
             "|" + "|".join(["---"] * len(header)) + "|"]

    for key in sorted(rows, key=lambda k: _profile_key(*k)):
        profile, conns = key
        vals = rows[key]
        cells = [f"{profile}/{conns}"]
        for c in columns:
            v = vals.get(c)
            cells.append(_fmt_num(v) if v is not None else "—")
        # Ratio columns: base / other (newest / older = speedup; >1.0x = improved)
        if ratio_base and ratio_base in columns:
            base_val = vals.get(ratio_base)
            for c in columns:
                if c == ratio_base or not c.startswith("BB "):
                    continue
                v = vals.get(c)
                if base_val and v and v > 0:
                    cells.append(f"{base_val / v:.2f}x")
                else:
                    cells.append("—")
        lines.append("| " + " | ".join(cells) + " |")

    return "# Framework comparison — merged\n\n" + "\n".join(lines) + "\n"


def render_csv(rows: dict, columns: list, ratio_base: str | None = None) -> str:
    """Render as CSV."""
    import io
    buf = io.StringIO()

    header = ["profile", "conns"] + columns
    if ratio_base and ratio_base in columns:
        base_short = ratio_base.split()[-1]
        for c in columns:
            if c != ratio_base and c.startswith("BB "):
                col_short = c.split()[-1]
                header.append(f"{base_short}/{col_short}")

    buf.write(",".join(f'"{h}"' for h in header) + "\n")

    for key in sorted(rows, key=lambda k: _profile_key(*k)):
        profile, conns = key
        vals = rows[key]
        row = [profile, str(conns)]
        for c in columns:
            v = vals.get(c)
            row.append(str(v) if v is not None else "")
        if ratio_base and ratio_base in columns:
            base_val = vals.get(ratio_base)
            for c in columns:
                if c == ratio_base or not c.startswith("BB "):
                    continue
                v = vals.get(c)
                if base_val and v and base_val > 0:
                    row.append(f"{base_val / v:.3f}")
                else:
                    row.append("")
        buf.write(",".join(f'"{x}"' for x in row) + "\n")

    return buf.getvalue()


def _pick_ratio_base(columns: list) -> str | None:
    """Pick the latest BB version as the ratio base."""
    bb_cols = [c for c in columns if c.startswith("BB ")]
    if len(bb_cols) < 2:
        return None
    # Sort by version and pick newest
    def _ver_key(c: str) -> tuple:
        try:
            parts = c.split()[-1].lstrip("v").split(".")
            return tuple(int(p) for p in parts)
        except (ValueError, IndexError):
            return (0,)
    bb_cols.sort(key=_ver_key, reverse=True)
    return bb_cols[0]


def main(argv: list) -> int:
    parser = argparse.ArgumentParser(
        description="Merge HttpArena results from multiple runs into one table"
    )
    parser.add_argument(
        "dirs", nargs="+",
        help="Result directories (containing httparena-tree/results/)"
    )
    parser.add_argument(
        "--labels", default=None,
        help="Comma-separated column labels (one per directory); "
             "overrides auto-detection. Use 'BB <version>' for BlackBull, "
             "or the framework name for others."
    )
    parser.add_argument(
        "--csv", action="store_true",
        help="Output CSV instead of markdown"
    )
    parser.add_argument(
        "-o", "--output", default=None,
        help="Write output to file (default: stdout)"
    )
    parser.add_argument(
        "--ratio-base", default=None,
        help="Explicit ratio base column label (default: latest BB version)"
    )
    args = parser.parse_args(argv[1:])

    # ── Collect data from all directories ─────────────────────────────────
    all_rows: list[dict] = []
    all_columns: set = set()

    # If --labels is provided, we need to pass them to the collection logic.
    # Each dir gets one label (for its BlackBull column); non-BB frameworks
    # keep their bare names.
    explicit_labels: list[str] = []
    if args.labels:
        explicit_labels = [lbl.strip() for lbl in args.labels.split(",")]
        if len(explicit_labels) != len(args.dirs):
            print(f"Error: got {len(explicit_labels)} labels for {len(args.dirs)} dirs",
                  file=sys.stderr)
            return 2

    for i, d in enumerate(args.dirs):
        d = d.rstrip("/")
        if not os.path.isdir(d):
            print(f"Error: not a directory: {d}", file=sys.stderr)
            return 1
        print(f"Collecting from: {d}", file=sys.stderr)
        rows, _ = _collect_from_dir(d)
        # If explicit labels given, remap the BB column in this dir's rows
        if explicit_labels:
            lbl = explicit_labels[i]
            remapped: dict = {}
            for key, vals in rows.items():
                new_vals = {}
                for col, rps in vals.items():
                    if col.startswith("BB "):
                        new_vals[lbl] = rps
                    else:
                        new_vals[col] = rps
                remapped[key] = new_vals
            rows = remapped
        all_rows.append(rows)
        # Collect all column names seen
        for vals in rows.values():
            all_columns.update(vals.keys())

    # ── Merge ─────────────────────────────────────────────────────────────
    merged = _merge_rows(all_rows)

    # Build column list — stable order: BB columns by version, then others alphabetically
    def _col_key(c: str) -> tuple:
        if c.startswith("BB "):
            try:
                parts = c.split()[-1].lstrip("v").split(".")
                return (0, tuple(int(p) for p in parts))
            except (ValueError, IndexError):
                return (0, (0,))
        return (1, c)
    columns = sorted(all_columns, key=_col_key)

    if not columns:
        print("Error: no data collected from any directory", file=sys.stderr)
        return 1

    print(f"Columns: {columns}", file=sys.stderr)

    # ── Ratio base ─────────────────────────────────────────────────────────
    ratio_base = args.ratio_base or _pick_ratio_base(columns)
    if ratio_base:
        print(f"Ratio base: {ratio_base}", file=sys.stderr)

    # ── Render ─────────────────────────────────────────────────────────────
    if args.csv:
        output = render_csv(merged, columns, ratio_base)
    else:
        output = render_markdown(merged, columns, ratio_base)

    if args.output:
        with open(args.output, "w") as fh:
            fh.write(output)
        print(f"Wrote {args.output}", file=sys.stderr)
    else:
        print(output)

    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
