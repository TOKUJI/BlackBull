"""Aggregate per-run numbers from a wrk multi-run.

Sprint 24 : the wrk/oha lanes now run
RUNS times per scenario and report a robust set of stats:

    median  median-absolute-deviation  min  max  noise_pct

`noise_pct = MAD / median * 100`.  Rows where this exceeds 10 % carry a
🌫 marker in the markdown report — a visible flag that the number is in
the noise band and should not be cited for sub-10 % comparisons.

Usage::

    python bench/wrk/_stats.py 15793.22 15812.40 15750.11

prints one whitespace-separated line, easy for the bash caller to read::

    15793.22 31.06 15750.11 15812.40 0.20

Empty / non-numeric inputs are skipped; if no valid samples remain,
prints '— — — — —' so the caller still gets five tokens.
"""
from __future__ import annotations

import sys
from statistics import median


def parse(args: list[str]) -> list[float]:
    out: list[float] = []
    for a in args:
        a = a.strip()
        if not a:
            continue
        try:
            out.append(float(a))
        except ValueError:
            continue
    return out


def main(argv: list[str]) -> None:
    vals = parse(argv[1:])
    if not vals:
        print("— — — — —")
        return
    med = median(vals)
    mad = median(abs(v - med) for v in vals) if len(vals) > 1 else 0.0
    lo, hi = min(vals), max(vals)
    noise_pct = (mad / med * 100.0) if med else 0.0
    # Format: median with same precision as wrk (2 decimals when small,
    # integer for >= 100).  Match the convention summarize.py already uses.
    def fmt(v: float) -> str:
        return f"{v:.0f}" if abs(v) >= 100 else f"{v:.2f}"
    print(f"{fmt(med)} {fmt(mad)} {fmt(lo)} {fmt(hi)} {noise_pct:.1f}")


if __name__ == "__main__":
    main(sys.argv)
