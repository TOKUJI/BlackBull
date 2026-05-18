#!/usr/bin/env python3
"""Parse h2load_run.sh raw output and write a markdown result file.

Usage:
    python bench/summarize.py bench/results/raw_20260517-201141.txt
Writes:
    bench/results/20260517-201141.md
"""
import re
import statistics
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class StatRow:
    """min / mean / sd / max — all in milliseconds."""
    min: float = 0.0
    mean: float = 0.0
    sd: float = 0.0
    max: float = 0.0


@dataclass
class ScenarioResult:
    label: str = ""
    # "finished in" line
    run_s: float = 0.0
    req_s: float = 0.0        # total req/s
    tput_mbs: float = 0.0     # MB/s (0 = sub-KB, shown as —)
    # request counts
    total: int = 0
    succeeded: int = 0
    failed: int = 0
    errored: int = 0
    # stat rows
    latency: StatRow = field(default_factory=StatRow)    # time for request
    connect: StatRow = field(default_factory=StatRow)    # time for connect
    ttfb: StatRow = field(default_factory=StatRow)       # time to 1st byte
    conn_rps: StatRow = field(default_factory=StatRow)   # req/s per connection
    # TLS
    tls_proto: str = ""
    cipher: str = ""

    # multi-run aggregation fields
    run_count: int = 1
    spread_pct: float | None = None  # (max-min)/median req/s × 100

    @property
    def tls_pct(self) -> float:
        """Max connect time as % of total run time — proxy for TLS-noise ratio."""
        if self.run_s <= 0:
            return 0.0
        return self.connect.max / (self.run_s * 1000) * 100


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------

def _ms(token: str) -> float:
    """'250us' → 0.25, '4.02ms' → 4.02, '1.35s' → 1350.0"""
    t = token.strip()
    if t.endswith("us"):
        return float(t[:-2]) / 1000
    if t.endswith("ms"):
        return float(t[:-2])
    if t.endswith("s"):
        return float(t[:-1]) * 1000
    return float(t)


def _mbs(token: str) -> float:
    """'494.83KB/s' → 0.483, '12.73MB/s' → 12.73, '1.96GB/s' → 2007.0"""
    t = token.strip()
    if t.endswith("GB/s"):
        return float(t[:-4]) * 1024
    if t.endswith("MB/s"):
        return float(t[:-4])
    if t.endswith("KB/s"):
        return float(t[:-4]) / 1024
    if t.endswith("B/s"):
        return float(t[:-3]) / (1024 * 1024)
    return 0.0


def _stat(tokens: list[str]) -> StatRow:
    """tokens[0..3] = min max mean sd (order as h2load prints them)."""
    return StatRow(
        min=_ms(tokens[0]),
        max=_ms(tokens[1]),
        mean=_ms(tokens[2]),
        sd=_ms(tokens[3]),
    )


def _stat_plain(tokens: list[str]) -> StatRow:
    """For 'req/s :' rows where values are plain floats, not durations."""
    return StatRow(
        min=float(tokens[0]),
        max=float(tokens[1]),
        mean=float(tokens[2]),
        sd=float(tokens[3]),
    )


# ---------------------------------------------------------------------------
# Main parser
# ---------------------------------------------------------------------------

def parse(text: str) -> tuple[dict[str, str], list[ScenarioResult]]:
    """Return (env dict, list of ScenarioResult)."""
    env: dict[str, str] = {}
    results: list[ScenarioResult] = []
    cur: ScenarioResult | None = None

    for line in text.splitlines():

        # --- scenario label ---
        m = re.match(r"^---\s+(.+?)\s+---$", line)
        if m:
            if cur is not None:
                results.append(cur)
            cur = ScenarioResult(label=m.group(1).strip())
            continue

        # env / config header lines  (key : value) — emitted by h2load_run.sh header
        m = re.match(r"^(Date|Workers|uvloop|Streams \w+)\s+:\s+(.+)$", line)
        if m and cur is None:
            env[m.group(1).strip()] = m.group(2).strip()
            continue

        if cur is None:
            continue

        # TLS Protocol / Cipher
        m = re.match(r"^TLS Protocol:\s+(.+)$", line)
        if m:
            cur.tls_proto = m.group(1).strip()
            continue
        m = re.match(r"^Cipher:\s+(.+)$", line)
        if m:
            cur.cipher = m.group(1).strip()
            continue

        # finished in X.XXs (or Xms), Y req/s, Z MB/s
        m = re.match(r"^finished in ([\d.]+)(ms|s),\s+([\d.]+)\s+req/s,\s+(\S+)$", line)
        if m:
            raw_t, unit = float(m.group(1)), m.group(2)
            cur.run_s = raw_t / 1000 if unit == "ms" else raw_t
            cur.req_s = float(m.group(3))
            cur.tput_mbs = _mbs(m.group(4))
            continue

        # requests: N total ... N succeeded, N failed, N errored
        m = re.match(
            r"^requests:\s+(\d+)\s+total.*?(\d+)\s+succeeded,\s+(\d+)\s+failed,\s+(\d+)\s+errored",
            line,
        )
        if m:
            cur.total, cur.succeeded, cur.failed, cur.errored = (
                int(m.group(1)), int(m.group(2)), int(m.group(3)), int(m.group(4))
            )
            continue

        # time for request:  min  max  mean  sd  +/-sd%
        m = re.match(r"^time for request:\s+(\S+)\s+(\S+)\s+(\S+)\s+(\S+)", line)
        if m:
            cur.latency = _stat(list(m.groups()))
            continue

        m = re.match(r"^time for connect:\s+(\S+)\s+(\S+)\s+(\S+)\s+(\S+)", line)
        if m:
            cur.connect = _stat(list(m.groups()))
            continue

        m = re.match(r"^time to 1st byte:\s+(\S+)\s+(\S+)\s+(\S+)\s+(\S+)", line)
        if m:
            cur.ttfb = _stat(list(m.groups()))
            continue

        # req/s           :  min  max  mean  sd  +/-sd%
        m = re.match(r"^req/s\s+:\s+(\S+)\s+(\S+)\s+(\S+)\s+(\S+)", line)
        if m:
            cur.conn_rps = _stat_plain(list(m.groups()))
            continue

    if cur is not None:
        results.append(cur)

    return env, results


# ---------------------------------------------------------------------------
# Multi-run aggregation
# ---------------------------------------------------------------------------

def aggregate_runs(results: list[ScenarioResult]) -> list[ScenarioResult]:
    """Group same-label runs; return one median-representative result per label."""
    if not results:
        return results

    groups: dict[str, list[ScenarioResult]] = defaultdict(list)
    order: list[str] = []
    for r in results:
        if r.label not in groups:
            order.append(r.label)
        groups[r.label].append(r)

    agg: list[ScenarioResult] = []
    for label in order:
        runs = groups[label]
        if len(runs) == 1:
            runs[0].run_count = 1
            agg.append(runs[0])
            continue
        rates = [r.req_s for r in runs]
        med = statistics.median(rates)
        spread = (max(rates) - min(rates)) / med * 100 if med > 0 else 0.0
        rep = min(runs, key=lambda r: abs(r.req_s - med))
        rep.run_count = len(runs)
        rep.spread_pct = spread
        agg.append(rep)
    return agg


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------

def _fmt_ms(v: float) -> str:
    if v == 0:
        return "—"
    if v < 1.0:
        return f"{v * 1000:.0f} µs"
    if v < 100.0:
        return f"{v:.2f} ms"
    return f"{v:.1f} ms"


def _fmt_rps(v: float) -> str:
    return f"{v:,.0f}" if v >= 1000 else f"{v:.1f}"


def _fmt_mbs(v: float) -> str:
    if v == 0:
        return "—"
    if v >= 100:
        return f"{v:.0f} MB/s"
    if v >= 1:
        return f"{v:.1f} MB/s"
    return f"{v * 1024:.0f} KB/s"


def _tls_flag(r: ScenarioResult) -> str:
    return " ⚠" if r.tls_pct > 2.0 else ""


# ---------------------------------------------------------------------------
# Markdown renderer
# ---------------------------------------------------------------------------

def render(env: dict[str, str], results: list[ScenarioResult], stem: str) -> str:
    ts = stem.replace("raw_", "")
    out: list[str] = [
        f"# h2load results — {ts}",
        "",
        "## Environment",
        "",
    ]
    if env:
        for k, v in env.items():
            out.append(f"- **{k}**: `{v}`")
    else:
        out.append("_(no environment header found in raw log)_")

    # ---- summary table ----
    multi_run = any(r.run_count > 1 for r in results)
    if multi_run:
        out += [
            "",
            "## Summary",
            "",
            "| Scenario | req/s | spread | mean lat | ±sd | max lat | throughput | run s | TLS% | failed |",
            "|---|---|---|---|---|---|---|---|---|---|",
        ]
    else:
        out += [
            "",
            "## Summary",
            "",
            "| Scenario | req/s | mean lat | ±sd | max lat | throughput | run s | TLS% | failed |",
            "|---|---|---|---|---|---|---|---|---|",
        ]
    for r in results:
        flag = _tls_flag(r)
        if multi_run:
            if r.spread_pct is not None:
                spread_str = f"{r.spread_pct:.1f}% ({r.run_count}×)"
                if r.spread_pct > 10:
                    spread_str += " ⚠"
            else:
                spread_str = "—"
            out.append(
                f"| {r.label} "
                f"| {_fmt_rps(r.req_s)} "
                f"| {spread_str} "
                f"| {_fmt_ms(r.latency.mean)} "
                f"| {_fmt_ms(r.latency.sd)} "
                f"| {_fmt_ms(r.latency.max)} "
                f"| {_fmt_mbs(r.tput_mbs)} "
                f"| {r.run_s:.2f} "
                f"| {r.tls_pct:.1f}%{flag} "
                f"| {'⚠ ' + str(r.failed) if r.failed else '0'} |"
            )
        else:
            out.append(
                f"| {r.label} "
                f"| {_fmt_rps(r.req_s)} "
                f"| {_fmt_ms(r.latency.mean)} "
                f"| {_fmt_ms(r.latency.sd)} "
                f"| {_fmt_ms(r.latency.max)} "
                f"| {_fmt_mbs(r.tput_mbs)} "
                f"| {r.run_s:.2f} "
                f"| {r.tls_pct:.1f}%{flag} "
                f"| {'⚠ ' + str(r.failed) if r.failed else '0'} |"
            )

    # ---- per-scenario detail ----
    out += ["", "## Detailed results", ""]
    for r in results:
        out += [
            f"### {r.label}",
            "",
            "| Metric | min | mean | ±sd | max |",
            "|---|---|---|---|---|",
            f"| Request latency"
            f" | {_fmt_ms(r.latency.min)}"
            f" | {_fmt_ms(r.latency.mean)}"
            f" | {_fmt_ms(r.latency.sd)}"
            f" | {_fmt_ms(r.latency.max)} |",
            f"| Connect (TLS setup)"
            f" | {_fmt_ms(r.connect.min)}"
            f" | {_fmt_ms(r.connect.mean)}"
            f" | {_fmt_ms(r.connect.sd)}"
            f" | {_fmt_ms(r.connect.max)} |",
            f"| Time to first byte"
            f" | {_fmt_ms(r.ttfb.min)}"
            f" | {_fmt_ms(r.ttfb.mean)}"
            f" | {_fmt_ms(r.ttfb.sd)}"
            f" | {_fmt_ms(r.ttfb.max)} |",
            f"| req/s per connection"
            f" | {r.conn_rps.min:.1f}"
            f" | {r.conn_rps.mean:.1f}"
            f" | ±{r.conn_rps.sd:.1f}"
            f" | {r.conn_rps.max:.1f} |",
            "",
            f"- **throughput**: {_fmt_mbs(r.tput_mbs)}",
            f"- **run time**: {r.run_s:.2f} s"
            f"  — TLS setup ≈ {r.tls_pct:.1f}% of run{_tls_flag(r)}",
            f"- **requests**: {r.total:,} total"
            f"  / {r.succeeded:,} succeeded"
            f"  / {r.failed} failed"
            f"  / {r.errored} errored",
        ]
        if r.tls_proto:
            out.append(f"- **TLS**: {r.tls_proto} / {r.cipher}")
        out.append("")

    # ---- legend ----
    out += [
        "---",
        "",
        "## Index legend",
        "",
        "| Index | Meaning |",
        "|---|---|",
        "| **req/s** | Total requests per second across all connections |",
        "| **mean lat** | Average time from sending request to receiving complete response |",
        "| **±sd** | Standard deviation of request latency — small = consistent, large = spiky |",
        "| **max lat** | Worst single request latency in the run |",
        "| **throughput** | Application-level data rate (headers excluded) |",
        "| **run s** | Total wall-clock duration of the scenario |",
        "| **TLS%** | `max_connect_time / run_time` — fraction spent on initial TLS handshakes; ⚠ if > 2% (measurement noise) |",
        "| **Connect min/mean/max** | TLS handshake duration per connection (paid once; not per-request) |",
        "| **TTFB** | Time from connection ready to first response byte — includes server queue time |",
        "| **req/s per connection** | Per-connection throughput distribution; high sd = uneven load across workers (SO_REUSEPORT skew) |",
        "",
    ]
    return "\n".join(out)


# ---------------------------------------------------------------------------
# k6 summary JSON parsing
# ---------------------------------------------------------------------------

import json as _json


@dataclass
class K6ScenarioResult:
    label: str = ""
    req_s: float = 0.0
    p50_ms: float = 0.0
    p95_ms: float = 0.0
    p99_ms: float = 0.0
    max_ms: float = 0.0
    error_rate: float = 0.0
    total_reqs: int = 0
    h2_rate: float | None = None  # fraction of requests served over HTTP/2 (None = not tracked)
    run_count: int = 1
    spread_pct: float | None = None  # (max-min)/median req/s × 100 across stress runs


def parse_k6_summary(path: Path, label: str) -> K6ScenarioResult:
    """Parse a k6 --summary-export JSON file into a K6ScenarioResult.

    k6's summary-export places metric values as direct fields (not nested under
    'values'), e.g. metrics.http_reqs.rate and metrics.http_req_duration['p(99)'].
    """
    data = _json.loads(path.read_text())
    metrics = data.get("metrics", {})

    r = K6ScenarioResult(label=label)

    reqs = metrics.get("http_reqs", {})
    r.req_s = float(reqs.get("rate", 0.0))
    r.total_reqs = int(reqs.get("count", 0))

    dur = metrics.get("http_req_duration", {})
    r.p50_ms = float(dur.get("p(50)", dur.get("med", 0.0)))
    r.p95_ms = float(dur.get("p(95)", 0.0))
    r.p99_ms = float(dur.get("p(99)", 0.0))
    r.max_ms = float(dur.get("max", 0.0))

    # errors is a custom Rate metric; k6 stores the rate in 'value'
    err = metrics.get("errors", {})
    r.error_rate = float(err.get("value", 0.0))

    # http2_ok is a custom Rate metric added by the k6 scripts
    h2 = metrics.get("http2_ok", {})
    if h2:
        r.h2_rate = float(h2.get("value", 0.0))

    return r


def aggregate_k6_stress(runs: list[K6ScenarioResult]) -> K6ScenarioResult:
    """Return a single K6ScenarioResult with median metrics across stress runs."""
    if len(runs) == 1:
        r = runs[0]
        r.label = "stress (500 VU, /ping)"
        r.run_count = 1
        return r
    rates = [r.req_s for r in runs]
    med = statistics.median(rates)
    spread = (max(rates) - min(rates)) / med * 100 if med > 0 else 0.0
    h2_vals = [r.h2_rate for r in runs if r.h2_rate is not None]
    return K6ScenarioResult(
        label="stress (500 VU, /ping)",
        req_s=med,
        p50_ms=statistics.median([r.p50_ms for r in runs]),
        p95_ms=statistics.median([r.p95_ms for r in runs]),
        p99_ms=statistics.median([r.p99_ms for r in runs]),
        max_ms=statistics.median([r.max_ms for r in runs]),
        error_rate=statistics.median([r.error_rate for r in runs]),
        total_reqs=int(statistics.median([r.total_reqs for r in runs])),
        h2_rate=statistics.median(h2_vals) if h2_vals else None,
        run_count=len(runs),
        spread_pct=spread,
    )


def render_k6(results: list[K6ScenarioResult]) -> str:
    """Return a markdown section to append to an existing summary file."""
    has_h2 = any(r.h2_rate is not None for r in results)
    has_spread = any(r.run_count > 1 for r in results)

    def _spread_str(r: K6ScenarioResult) -> str:
        if r.spread_pct is None:
            return "—"
        s = f"{r.spread_pct:.1f}% ({r.run_count}×)"
        return s + " ⚠" if r.spread_pct > 10 else s

    cols = ["Scenario"]
    if has_h2:
        cols.append("proto")
    cols += ["req/s"]
    if has_spread:
        cols.append("spread")
    cols += ["p50", "p95", "p99", "max", "errors", "requests"]

    header = "| " + " | ".join(cols) + " |"
    sep    = "|" + "|".join("---" for _ in cols) + "|"

    out: list[str] = ["", "---", "", "## k6 results", "", header, sep]
    for r in results:
        err_str = f"{r.error_rate * 100:.2f}%" if r.error_rate > 0 else "0%"
        cells = [r.label]
        if has_h2:
            if r.h2_rate is None:
                cells.append("—")
            elif r.h2_rate >= 0.999:
                cells.append("HTTP/2 ✓")
            else:
                cells.append(f"HTTP/2 {r.h2_rate * 100:.1f}% ⚠")
        cells.append(_fmt_rps(r.req_s))
        if has_spread:
            cells.append(_spread_str(r))
        cells += [
            _fmt_ms(r.p50_ms),
            _fmt_ms(r.p95_ms),
            _fmt_ms(r.p99_ms),
            _fmt_ms(r.max_ms),
            err_str,
            f"{r.total_reqs:,}",
        ]
        out.append("| " + " | ".join(cells) + " |")
    out.append("")
    return "\n".join(out)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    if len(sys.argv) < 2:
        _usage()

    if sys.argv[1] == "k6":
        _main_k6()
    else:
        _main_h2load()


def _usage() -> None:
    print(
        "Usage:\n"
        "  python bench/summarize.py <raw_h2load_log>\n"
        "  python bench/summarize.py k6 <rampup.json> <stress1.json> [stress2.json ...] <summary.md>",
        file=sys.stderr,
    )
    sys.exit(1)


def _main_h2load() -> None:
    raw = Path(sys.argv[1])
    env, results = parse(raw.read_text())
    results = aggregate_runs(results)
    stem = raw.stem.replace("raw_", "")
    out = raw.parent / f"{stem}.md"
    out.write_text(render(env, results, stem))
    print(f"Summary: {out}")


def _main_k6() -> None:
    # argv: k6 <rampup.json> <stress1.json> [stress2.json ...] <summary.md>
    if len(sys.argv) < 5:
        _usage()
    rampup_json = Path(sys.argv[2])
    md_file     = Path(sys.argv[-1])          # last arg is the .md output file
    stress_paths = [Path(p) for p in sys.argv[3:-1]]

    stress_runs = [
        parse_k6_summary(p, f"stress run {i+1}")
        for i, p in enumerate(stress_paths)
    ]
    stress_agg = aggregate_k6_stress(stress_runs)

    k6_results = [
        parse_k6_summary(rampup_json, "rampup (0→200 VU, /ping)"),
        stress_agg,
    ]

    md_file.write_text(md_file.read_text() + render_k6(k6_results))
    print(f"k6 results appended to: {md_file}")


if __name__ == "__main__":
    main()
