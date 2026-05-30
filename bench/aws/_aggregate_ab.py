"""Cross-pair aggregator for full_ab.sh runs.

Reads each pair's six wrk.md tables (uvicorn-pre, blackbull-base-u0,
blackbull-trt-u0, blackbull-base-u1, blackbull-trt-u1, uvicorn-post)
and produces:

1. Per-scenario paired delta (trt - base) per pair, per uvloop setting.
2. Cross-pair median paired-delta + IQR (the systematic-noise-robust
   point estimate).
3. uvicorn pre-post drift per pair (the host-state-change ruler).
4. A flag per scenario when the uvicorn drift swamps the blackbull
   delta.

Usage::

    python3 _aggregate_ab.py --root <full_ab_out_root> --pairs "1 2 3"
"""
from __future__ import annotations

import argparse
import re
import statistics
import sys
from pathlib import Path

ROW_RE = re.compile(
    r'^\|\s*([BE]\d+_\S+?)\s*\|\s*(\d+)\s*\|.*MAD\s+([\d.]+),\s+([\d.]+)%',
)


def parse_wrk_md(path: Path) -> dict[str, tuple[int, float, float]]:
    """Return {scenario: (req_per_s_median, mad, mad_pct)} or empty dict."""
    if not path.is_file():
        return {}
    rows: dict[str, tuple[int, float, float]] = {}
    with path.open() as f:
        for line in f:
            m = ROW_RE.match(line)
            if m:
                rows[m.group(1)] = (int(m.group(2)), float(m.group(3)), float(m.group(4)))
    return rows


def median(xs: list[float]) -> float:
    return statistics.median(xs) if xs else float('nan')


def iqr(xs: list[float]) -> tuple[float, float]:
    if len(xs) < 2:
        return (float('nan'), float('nan'))
    xs2 = sorted(xs)
    q1 = statistics.median(xs2[: len(xs2) // 2])
    q3 = statistics.median(xs2[(len(xs2) + 1) // 2:])
    return q1, q3


def pct(num: float, denom: float) -> float:
    return (num / denom) * 100.0 if denom else float('nan')


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument('--root', required=True)
    p.add_argument('--pairs', required=True, help='Space-separated pair IDs')
    args = p.parse_args()

    root = Path(args.root)
    pair_ids = args.pairs.split()

    # Load every pair's six phase results.
    phases = [
        'uvicorn-pre',
        'blackbull-base-u0', 'blackbull-trt-u0',
        'blackbull-base-u1', 'blackbull-trt-u1',
        'uvicorn-post',
    ]
    # {pair_id: {phase: {scenario: (rps, mad, mad_pct)}}}
    data: dict[str, dict[str, dict[str, tuple[int, float, float]]]] = {}
    for pid in pair_ids:
        data[pid] = {}
        for phase in phases:
            data[pid][phase] = parse_wrk_md(root / f'pair-{pid}' / phase / 'wrk.md')

    # Discover the set of scenarios actually measured (use blackbull-base-u0 as canonical).
    scenarios: set[str] = set()
    for pid in pair_ids:
        scenarios.update(data[pid].get('blackbull-base-u0', {}))
    scenarios_sorted = sorted(scenarios)

    print(f"# Cross-pair A/B aggregate — {root.name}")
    print()
    print(f"Pairs analysed: {', '.join(pair_ids)} (M={len(pair_ids)})")
    print()

    # --- Per-pair deltas ---
    print("## Per-pair deltas")
    for pid in pair_ids:
        print(f"\n### Pair {pid}")
        b0 = data[pid].get('blackbull-base-u0', {})
        t0 = data[pid].get('blackbull-trt-u0', {})
        b1 = data[pid].get('blackbull-base-u1', {})
        t1 = data[pid].get('blackbull-trt-u1', {})
        uv_pre = data[pid].get('uvicorn-pre', {})
        uv_post = data[pid].get('uvicorn-post', {})

        print()
        print("| Scenario | Base u0 | Trt u0 | Δ u0 | Base u1 | Trt u1 | Δ u1 | uv-pre | uv-post | Δ uv |")
        print("|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
        for scen in scenarios_sorted:
            bv0 = b0.get(scen, (0, 0, 0))[0]
            tv0 = t0.get(scen, (0, 0, 0))[0]
            bv1 = b1.get(scen, (0, 0, 0))[0]
            tv1 = t1.get(scen, (0, 0, 0))[0]
            uvp = uv_pre.get(scen, (0, 0, 0))[0]
            uvq = uv_post.get(scen, (0, 0, 0))[0]
            d0 = pct(tv0 - bv0, bv0) if bv0 else float('nan')
            d1 = pct(tv1 - bv1, bv1) if bv1 else float('nan')
            duv = pct(uvq - uvp, uvp) if uvp else float('nan')
            print(f"| {scen} | {bv0} | {tv0} | {d0:+.1f}% | {bv1} | {tv1} | {d1:+.1f}% | {uvp} | {uvq} | {duv:+.1f}% |")

    # --- Cross-pair aggregation ---
    print()
    print("## Cross-pair aggregate")
    print()
    print("**Paired delta** = (treatment − baseline) / baseline, per pair.  "
          "The MEDIAN across pairs is the point estimate; the IQR is the "
          "systematic-noise envelope.  "
          "**uv-drift** = |uvicorn-post − uvicorn-pre| / uvicorn-pre — "
          "host state change ruler for each pair.")
    print()
    print("| Scenario | Δ u0 median | Δ u0 IQR | Δ u1 median | Δ u1 IQR | uv-drift median | Verdict |")
    print("|---|---:|---:|---:|---:|---:|---|")

    for scen in scenarios_sorted:
        d0s = []
        d1s = []
        uv_drifts = []
        for pid in pair_ids:
            b0 = data[pid].get('blackbull-base-u0', {}).get(scen, (0, 0, 0))[0]
            t0 = data[pid].get('blackbull-trt-u0', {}).get(scen, (0, 0, 0))[0]
            b1 = data[pid].get('blackbull-base-u1', {}).get(scen, (0, 0, 0))[0]
            t1 = data[pid].get('blackbull-trt-u1', {}).get(scen, (0, 0, 0))[0]
            uvp = data[pid].get('uvicorn-pre', {}).get(scen, (0, 0, 0))[0]
            uvq = data[pid].get('uvicorn-post', {}).get(scen, (0, 0, 0))[0]
            if b0:
                d0s.append(pct(t0 - b0, b0))
            if b1:
                d1s.append(pct(t1 - b1, b1))
            if uvp:
                uv_drifts.append(abs(pct(uvq - uvp, uvp)))

        d0_med = median(d0s)
        d0_q1, d0_q3 = iqr(d0s)
        d1_med = median(d1s)
        d1_q1, d1_q3 = iqr(d1s)
        uv_med = median(uv_drifts)

        # Verdict: the change is "resolved" if |delta median| > uv-drift median.
        # Otherwise the host-state ruler swamps the signal.
        max_blackbull = max(abs(d0_med), abs(d1_med))
        if max_blackbull > 2 * uv_med:
            verdict = "✓ signal > 2× host drift"
        elif max_blackbull > uv_med:
            verdict = "△ signal ~ host drift"
        else:
            verdict = "✗ buried in host drift"

        print(f"| {scen} | {d0_med:+.1f}% | [{d0_q1:+.1f}%, {d0_q3:+.1f}%] | "
              f"{d1_med:+.1f}% | [{d1_q1:+.1f}%, {d1_q3:+.1f}%] | "
              f"{uv_med:+.1f}% | {verdict} |")

    print()
    print("## Interpretation guide")
    print()
    print("- **Δ u0 median**: median across pairs of (treatment − baseline)/baseline "
          "for `BB_UVLOOP=0`.  This is the point estimate of Phase A's effect "
          "on the parser-heavy workload.")
    print("- **Δ u0 IQR**: spread across pairs.  Wide IQR with narrow within-pair "
          "MAD means host-to-host variance is the dominant noise source.")
    print("- **uv-drift median**: how much uvicorn moved from pre-bench to "
          "post-bench on the same pair.  Sets the host-state ruler.  "
          "A blackbull delta meaningfully smaller than this is not "
          "resolvable.")
    print("- **Verdict**: ✓ when |median blackbull delta| > 2× host-drift; "
          "△ when it's within a factor of 2; ✗ when host drift is the "
          "dominant signal.")
    return 0


if __name__ == '__main__':
    sys.exit(main())
