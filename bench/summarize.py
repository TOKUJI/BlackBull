#!/usr/bin/env python3
"""Parse h2load_run.sh raw output and write a markdown result file.

Usage:
    python bench/summarize.py bench/results/raw_20260517-201141.txt
Writes:
    bench/results/20260517-201141.md
"""
import re
import sys
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
    out += [
        "",
        "## Summary",
        "",
        "| Scenario | req/s | mean lat | ±sd | max lat | throughput | run s | TLS% | failed |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for r in results:
        flag = _tls_flag(r)
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


def render_k6(results: list[K6ScenarioResult]) -> str:
    """Return a markdown section to append to an existing summary file."""
    has_h2 = any(r.h2_rate is not None for r in results)
    header = "| Scenario | proto | req/s | p50 | p95 | p99 | max | errors | requests |"
    sep    = "|---|---|---|---|---|---|---|---|---|"
    if not has_h2:
        header = "| Scenario | req/s | p50 | p95 | p99 | max | errors | requests |"
        sep    = "|---|---|---|---|---|---|---|---|"

    out: list[str] = ["", "---", "", "## k6 results", "", header, sep]
    for r in results:
        err_str = f"{r.error_rate * 100:.2f}%" if r.error_rate > 0 else "0%"
        if has_h2:
            if r.h2_rate is None:
                h2_str = "—"
            elif r.h2_rate >= 0.999:
                h2_str = "HTTP/2 ✓"
            else:
                h2_str = f"HTTP/2 {r.h2_rate * 100:.1f}% ⚠"
            out.append(
                f"| {r.label} "
                f"| {h2_str} "
                f"| {_fmt_rps(r.req_s)} "
                f"| {_fmt_ms(r.p50_ms)} "
                f"| {_fmt_ms(r.p95_ms)} "
                f"| {_fmt_ms(r.p99_ms)} "
                f"| {_fmt_ms(r.max_ms)} "
                f"| {err_str} "
                f"| {r.total_reqs:,} |"
            )
        else:
            out.append(
                f"| {r.label} "
                f"| {_fmt_rps(r.req_s)} "
                f"| {_fmt_ms(r.p50_ms)} "
                f"| {_fmt_ms(r.p95_ms)} "
                f"| {_fmt_ms(r.p99_ms)} "
                f"| {_fmt_ms(r.max_ms)} "
                f"| {err_str} "
                f"| {r.total_reqs:,} |"
            )
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
        "  python bench/summarize.py k6 <rampup.json> <stress.json> <summary.md>",
        file=sys.stderr,
    )
    sys.exit(1)


def _main_h2load() -> None:
    raw = Path(sys.argv[1])
    env, results = parse(raw.read_text())
    stem = raw.stem.replace("raw_", "")
    out = raw.parent / f"{stem}.md"
    out.write_text(render(env, results, stem))
    print(f"Summary: {out}")


def _main_k6() -> None:
    if len(sys.argv) < 5:
        _usage()
    rampup_json = Path(sys.argv[2])
    stress_json  = Path(sys.argv[3])
    md_file      = Path(sys.argv[4])

    k6_results = [
        parse_k6_summary(rampup_json, "rampup (0→200 VU, /ping)"),
        parse_k6_summary(stress_json,  "stress (500 VU, /ping)"),
    ]

    md_file.write_text(md_file.read_text() + render_k6(k6_results))
    print(f"k6 results appended to: {md_file}")


if __name__ == "__main__":
    main()
