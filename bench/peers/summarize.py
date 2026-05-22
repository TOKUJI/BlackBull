"""Post-process a compare_servers report and prepend a side-by-side summary.

Reads a markdown report produced by compare_servers.sh, parses the per-stack
tables, and emits a summary section with:

- One row per stack on Lane A1 (HTTP/2 single-stream), B1 (HTTP/1.1 plaintext),
  C1/C2 (k6 stress), Lane D (WebSocket avg).
- A `WSL2-bound?` flag set when a stack's req/s on small-payload scenarios
  (B1, B3, B6, C1) clusters within ±10% — a heuristic for "this server is
  bottlenecked by the loopback/syscall floor, not its own code."

Usage:
    python bench/peers/summarize.py bench/results/compare_servers_<ts>.md

The summary is inserted right after the preamble (the `Duration:` line).
The script is idempotent — re-running on a report that already has a
summary replaces the existing one.
"""
import re
import sys
from pathlib import Path

# Scenarios used for WSL2-ceiling detection: small-payload, light-compute.
# If a stack's req/s clusters tightly across these, it's loopback-bound.
CEILING_SCENARIOS = ("B1_plaintext_c256", "B3_json_c256", "B6_echo_1kb")
# Spread threshold: (max - min) / mean below this = ceiling-bound.
# 15% catches the obvious cases (uvicorn 13.2%, blackbull 11.1% on
# the 2026-05-21 run) while excluding hypercorn/daphne which spread
# 30-40% (CPU-bound, not loopback-bound).
# Also require min req/s >= 10k — a stack below that is clearly CPU-bound
# regardless of spread, so the ceiling label would be misleading.
CEILING_SPREAD = 0.15
CEILING_MIN_REQS = 10000


def parse_value(s: str) -> float | None:
    """Parse '13391.74' / '88050.06' into float. Returns None for '—' / 'err'."""
    s = s.strip()
    if not s or s in ("—", "err"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def parse_ms(s: str) -> float | None:
    """Parse '18.42ms' / '430us' / '0.272 ms' into milliseconds."""
    s = s.strip().lower().replace(" ", "")
    if not s or s in ("—", "err"):
        return None
    m = re.match(r"([\d.]+)(us|ms|s)$", s)
    if not m:
        try:
            return float(s)
        except ValueError:
            return None
    v, unit = float(m.group(1)), m.group(2)
    return {"us": v / 1000, "ms": v, "s": v * 1000}[unit]


def parse_report(text: str) -> dict[str, dict]:
    """Return {stack: {lane: {scenario: row_cells}}}."""
    stacks: dict[str, dict] = {}
    current_stack = None
    current_lane = None
    for line in text.splitlines():
        m = re.match(r"^## (\S+)\s*$", line)
        if m:
            current_stack = m.group(1)
            stacks.setdefault(current_stack, {})
            current_lane = None
            continue
        m = re.match(r"^### \S+\s+—\s+(Lane \S+)", line)
        if m and current_stack:
            current_lane = m.group(1)
            stacks[current_stack].setdefault(current_lane, {})
            continue
        if current_stack and current_lane and line.startswith("|") and "—" not in line[:4]:
            # data row
            cells = [c.strip() for c in line.strip("|").split("|")]
            if cells and cells[0] not in ("Scenario", "msg/s", "---", "") and "---" not in cells[0]:
                stacks[current_stack][current_lane][cells[0]] = cells
    return stacks


def reqs(stacks, stack, lane, scenario, col=1):
    try:
        return parse_value(stacks[stack][lane][scenario][col])
    except (KeyError, IndexError):
        return None


def lane_d_avg(stacks, stack):
    """Lane D table is anonymous — single row, no scenario name."""
    try:
        rows = stacks[stack].get("Lane D", {})
        if not rows:
            return None
        # The only row in lane D doesn't have a scenario name; first cell is msg/s value
        for k, cells in rows.items():
            # cells: msg/s | avg | p50 | p95 | p99 | max
            if len(cells) >= 2:
                return parse_ms(cells[1]), parse_value(cells[0])
        return None
    except Exception:
        return None


def is_wsl2_bound(stacks, stack):
    """Return (bound: bool, spread_pct: float) on small-payload scenarios."""
    samples = []
    for sc in CEILING_SCENARIOS:
        v = reqs(stacks, stack, "Lane B-wrk", sc)
        if v is not None:
            samples.append(v)
    if len(samples) < len(CEILING_SCENARIOS):
        return False, 0.0
    spread = (max(samples) - min(samples)) / (sum(samples) / len(samples))
    bound = spread < CEILING_SPREAD and min(samples) >= CEILING_MIN_REQS
    return bound, spread


def render_summary(stacks: dict[str, dict]) -> str:
    out = ["", "## Summary — side-by-side", ""]
    out.append(f"All numbers from the per-stack tables below. Highest peer-column-wise"
               f" result **bolded**; nginx values _italicized_ (reference floor,"
               f" not a peer); `🧱` flags WSL2-loopback-bound (small-payload"
               f" req/s clusters within ±{int(CEILING_SPREAD*100)}%).")
    out.append("")

    # Build columns
    metrics = [
        ("Lane A1 mux1 mean (ms)",  lambda s: parse_ms((stacks.get(s, {}).get("Lane A", {}).get("A1_plaintext_mux1") or ["", "", ""])[2]) if "Lane A" in stacks.get(s, {}) else None,                "lower"),
        ("Lane A1 mux1 req/s",       lambda s: reqs(stacks, s, "Lane A",      "A1_plaintext_mux1") if "Lane A" in stacks.get(s, {}) else None,                                                       "higher"),
        ("Lane B1 plaintext req/s",  lambda s: reqs(stacks, s, "Lane B-wrk",  "B1_plaintext_c256"),                                                                                                   "higher"),
        ("Lane B3 json req/s",       lambda s: reqs(stacks, s, "Lane B-wrk",  "B3_json_c256"),                                                                                                        "higher"),
        ("Lane B6 echo-1k req/s",    lambda s: reqs(stacks, s, "Lane B-wrk",  "B6_echo_1kb"),                                                                                                         "higher"),
        ("Lane C2 500-VU p99 (ms)",  lambda s: parse_ms((stacks.get(s, {}).get("Lane C", {}).get("C2") or ["", "", "", "", "", ""])[5]),                                                              "lower"),
        ("Lane C2 500-VU req/s",     lambda s: reqs(stacks, s, "Lane C",      "C2", col=2),                                                                                                           "higher"),
    ]

    stack_order = ["blackbull", "uvicorn", "hypercorn", "granian", "daphne", "nginx"]
    present = [s for s in stack_order if s in stacks]
    # nginx is the reference floor — exclude it from best-column bolding
    # so the "winner" row reflects the ASGI peers, not nginx-static.
    REFERENCE_STACKS = {"nginx"}
    peers = [s for s in present if s not in REFERENCE_STACKS]

    # Header
    header = "| Metric | " + " | ".join(present) + " |"
    sep = "|---|" + "|".join(["---"] * len(present)) + "|"
    out.append(header)
    out.append(sep)

    for label, fn, direction in metrics:
        vals = {s: fn(s) for s in present}
        # Decide winner among peers only — nginx is the reference floor.
        defined_peers = {s: v for s, v in vals.items()
                         if v is not None and s in peers}
        if defined_peers:
            best = (max if direction == "higher" else min)(defined_peers,
                                                            key=defined_peers.get)
        else:
            best = None
        cells = []
        for s in present:
            v = vals[s]
            if v is None:
                cells.append("—")
                continue
            txt = f"{v:.0f}" if v >= 100 else f"{v:.2f}"
            if s == best:
                txt = f"**{txt}**"
            elif s in REFERENCE_STACKS:
                txt = f"_{txt}_"   # italicize the reference-floor value
            cells.append(txt)
        out.append(f"| {label} | " + " | ".join(cells) + " |")

    # WS RTT avg row
    ws_row = ["| Lane D WS avg RTT (ms)"]
    ws_vals = {s: lane_d_avg(stacks, s) for s in present}
    ws_avg_peers = {s: t[0] for s, t in ws_vals.items()
                    if t and t[0] is not None and s in peers}
    best_ws = min(ws_avg_peers, key=ws_avg_peers.get) if ws_avg_peers else None
    for s in present:
        t = ws_vals.get(s)
        if not t or t[0] is None:
            ws_row.append("—")
        else:
            txt = f"{t[0]:.3f}"
            if s == best_ws:
                txt = f"**{txt}**"
            elif s in REFERENCE_STACKS:
                txt = f"_{txt}_"
            ws_row.append(txt)
    out.append(" | ".join(ws_row) + " |")

    # WSL2-bound row
    bound_row = [f"| WSL2-bound (small-payload spread <{int(CEILING_SPREAD*100)}%)"]
    for s in present:
        bound, spread = is_wsl2_bound(stacks, s)
        if bound:
            bound_row.append(f"🧱 yes ({spread*100:.1f}%)")
        else:
            bound_row.append(f"no ({spread*100:.1f}%)" if spread else "—")
    out.append(" | ".join(bound_row) + " |")

    out.append("")
    out.append("WSL2-bound interpretation: such a stack's request rate is set by"
               " the syscall/TLS floor on Linux loopback, not by the server's"
               " own code path. On a real network its number will move; the"
               " others should hold roughly to the same ranking.")
    out.append("")
    return "\n".join(out)


SUMMARY_START = "## Summary — side-by-side"
SUMMARY_END_MARK = "WSL2-bound interpretation:"


def splice(report_text: str, summary: str) -> str:
    """Insert summary after the `Duration:` preamble line. Idempotent."""
    # Remove existing summary if present
    if SUMMARY_START in report_text:
        # Find the start of the summary block and the next `##` after it.
        start = report_text.index(SUMMARY_START)
        # Find next `## ` at the start of a line after start.
        next_section = re.search(r"^## (?!Summary)", report_text[start:], re.MULTILINE)
        if next_section:
            report_text = report_text[:start] + report_text[start + next_section.start():]
        else:
            report_text = report_text[:start]

    # Insert after "Duration:" line (the last preamble line) + one blank line
    m = re.search(r"(^Duration:.*$\n)", report_text, re.MULTILINE)
    if not m:
        # Fall back: prepend
        return summary + "\n" + report_text
    idx = m.end()
    return report_text[:idx] + "\n" + summary + report_text[idx:]


def main(argv):
    if len(argv) != 2:
        print(f"usage: {argv[0]} <report.md>", file=sys.stderr)
        return 1
    path = Path(argv[1])
    text = path.read_text()
    stacks = parse_report(text)
    summary = render_summary(stacks)
    spliced = splice(text, summary)
    path.write_text(spliced)
    print(f"Wrote summary into {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
