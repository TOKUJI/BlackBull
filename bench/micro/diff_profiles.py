#!/usr/bin/env python3
"""Diff two cProfile dumps by cumulative time, keyed by function name.

Normalises by each dump's TOTAL CPU time (s.total_tt) — not the sum of
cumulative times, which double-counts children.  Fixed n per lane makes the
raw cumulative-second diff directly interpretable as per-request growth.
"""
import pstats
import sys


def snapshot(path):
    s = pstats.Stats(path)
    total = s.total_tt  # total CPU seconds for the whole run
    rows = {}
    for fn, (cc, nc, tt, ct, callers) in s.stats.items():
        name = f"{fn[0]}:{fn[1]}({fn[2]})"
        rows[name] = (tt, ct)
    return rows, total


def main():
    base = sys.argv[1]
    treat = sys.argv[2]
    top = int(sys.argv[3]) if len(sys.argv) > 3 else 40
    b, b_total = snapshot(base)
    t, t_total = snapshot(treat)
    keys = set(b) | set(t)
    rows = []
    for k in keys:
        btt, bct = b.get(k, (0.0, 0.0))
        ttt, tct = t.get(k, (0.0, 0.0))
        rows.append((abs(tct - bct), tct - bct, k, bct, tct, btt, ttt))
    rows.sort(reverse=True)
    print(f"base total CPU = {b_total:.3f}s   treat total CPU = {t_total:.3f}s")
    print(f"{'Δcum(s)':>10} {'base_ct':>9} {'treat_ct':>9} {'treat_tt':>9}  function")
    for d, delta, k, bct, tct, btt, ttt in rows[:top]:
        print(f"{delta:>+10.4f} {bct:>9.4f} {tct:>9.4f} {ttt:>9.4f}  {k}")


if __name__ == "__main__":
    main()
