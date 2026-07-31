#!/usr/bin/env python3
"""Equivalence (TOST) analysis for an ab_commit.sh raw.tsv.

Answers the question "is the real delta within +/-EQUIV % with 95 %
confidence?" — i.e. it tries to *reject* "there is a difference", not to
detect one.  Detection power is ab_report.py's job; this is the companion
that lets you *claim* no meaningful difference on a specific box.

Method
------
The box is bimodal: each server restart lands the process in a fast or a
slow state (~15 % apart) and holds it.  Comparing base vs treat *across*
modes therefore measures the coin toss, not the commits.  So:

1. Per phase, split samples into slow/fast using the midpoint of the
   observed range (the same diagnostic ab_report.py uses for `hi-mode`).
2. Per mode, estimate the log-scale delta (base vs treat) with a Welch SE.
3. Combined estimate: inverse-variance weighted delta across modes, with a
   Satterthwaite df.  This is the estimator that converges under bimodality.
4. TOST: equivalence within +/-EQUIV is declared when the 95 % CI of the
   combined delta fits strictly inside (-log1p(EQUIV), +log1p(EQUIV)).
5. A conservative variant requires *both* modes to pass TOST individually.

A/A null phase: it reports the same verdict on byte-identical arms, so its
CI is the floor the real phase has to beat.  If the null phase already fails
equivalence, no number of real rounds can claim it on this box.

Usage:
    uv run python bench/peers/ab_equiv_report.py <raw.tsv> [equiv%]

    equiv%  equivalence bound, percent, default 0.5 (i.e. +/-0.5 %)
"""
from __future__ import annotations

import math
import sys
from pathlib import Path

import numpy as np
from scipy import stats

ALPHA = 0.025  # one-sided per TOST leg -> 95 % CI


def _load(raw: Path) -> dict[str, dict[str, list[float]]]:
    out: dict[str, dict[str, list[float]]] = {}
    for line in raw.read_text().splitlines()[1:]:
        parts = line.split('\t')
        if len(parts) < 4:
            continue
        phase, _round, arm, rps = parts[:4]
        try:
            out.setdefault(phase, {}).setdefault(arm, []).append(float(rps))
        except ValueError:
            continue
    return out


def _split_point(vals: list[float]) -> float:
    """Midpoint of the observed range — enough to label the two clusters
    when they are as far apart as they are here (~15 %)."""
    return (min(vals) + max(vals)) / 2


def _log_delta(bm: list[float], tm: list[float]):
    """Log-scale (base vs treat) delta + Welch SE + Welch df."""
    lb = np.log(np.asarray(bm, float))
    lt = np.log(np.asarray(tm, float))
    nt, nb = len(lt), len(lb)
    vt, vb = lt.var(ddof=1), lb.var(ddof=1)
    d = lt.mean() - lb.mean()
    se = math.sqrt(vt / nt + vb / nb)
    df = (vt / nt + vb / nb) ** 2 / (
        (vt / nt) ** 2 / (nt - 1) + (vb / nb) ** 2 / (nb - 1))
    return d, se, df


def _pct(x: float) -> str:
    return f'{(math.expm1(x)) * 100:+.2f}%'


def _tost_ok(d: float, se: float, df: float, e: float) -> bool:
    """|d| + t_{1-ALPHA, df} * se < e  ->  95 % CI strictly inside +/-e."""
    crit = stats.t.ppf(1 - ALPHA, df) if df > 0 else stats.norm.ppf(1 - ALPHA)
    return abs(d) + crit * se < e


def _analyse(phase: str, data: dict[str, list[float]], e: float):
    arms = {a: data.get(a, []) for a in ('base', 'treat')}
    if not arms['base'] or not arms['treat']:
        return
    allv = arms['base'] + arms['treat']
    thr = _split_point(allv)

    # per-mode slow/fast membership, per arm
    modes: dict[str, dict[str, list[float]]] = {'slow': {}, 'fast': {}}
    hi_tot = 0
    for arm in ('base', 'treat'):
        xs = arms[arm]
        slow = [x for x in xs if x <= thr]
        fast = [x for x in xs if x > thr]
        modes['slow'][arm] = slow
        modes['fast'][arm] = fast
        hi_tot += len(fast)

    print(f'\n== {phase} phase ==')
    print(f'{"mode":5s} {"arm":5s} {"n":>3s} {"mean":>9s} {"SE":>7s}  TOST(pass?)')
    deltas: dict[str, tuple[float, float, float]] = {}
    mixed = 0 < hi_tot < len(allv)
    for mode in ('slow', 'fast'):
        bm, tm = modes[mode]['base'], modes[mode]['treat']
        if len(bm) < 2 or len(tm) < 2:
            print(f'{mode:5s} {"-":5s} {"<2 samples — skipped":>30s}')
            continue
        d, se, df = _log_delta(bm, tm)
        ok = _tost_ok(d, se, df, e)
        deltas[mode] = (d, se, df)
        print(f'{mode:5s} base  {len(bm):3d} {np.mean(bm):9.0f} '
              f'{np.std(bm, ddof=1)/math.sqrt(len(bm)):7.0f}')
        print(f'{mode:5s} treat {len(tm):3d} {np.mean(tm):9.0f} '
              f'{np.std(tm, ddof=1)/math.sqrt(len(tm)):7.0f}'
              f'   d={_pct(d)}  {"pass" if ok else "FAIL"}')
    if not deltas:
        print('  insufficient data for any mode')
        return

    # combined: inverse-variance weighted across modes
    w = {m: 1.0 / s ** 2 for m, (_, s, _) in deltas.items()}
    dhat = sum(d * w[m] for m, (d, _, _) in deltas.items()) / sum(w.values())
    se_hat = 1.0 / math.sqrt(sum(w.values()))
    df_hat = (sum(w.values())) ** 2 / sum(
        w[m] ** 2 / df for m, (_, _, df) in deltas.items())
    crit = stats.t.ppf(1 - ALPHA, df_hat)
    lo, hi = dhat - crit * se_hat, dhat + crit * se_hat
    comb_ok = abs(dhat) + crit * se_hat < e

    both_ok = len(deltas) >= 2 and all(
        _tost_ok(d, s, df, e) for d, s, df in deltas.values())

    print(f'\n  combined delta  : {_pct(dhat)}')
    print(f'  95 % CI (df={df_hat:.0f}): [{_pct(lo)}, {_pct(hi)}]')
    print(f'  modes used      : {", ".join(deltas)}')
    if mixed:
        print('  * sample is mixed-mode (hi-mode neither 0/n nor n/n) — '
              'combined estimator is required')
    print(f'\n  equivalence within +/-{math.expm1(e)*100:g}%: '
          f'{"YES" if comb_ok else "NO"} (combined)  '
          f'{"YES" if both_ok else "NO"} (both modes)')
    print(f'  verdict: {"equivalent" if comb_ok else "NOT equivalent"}')


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(__doc__)
        return 2
    raw = Path(argv[1])
    equiv = float(argv[2]) / 100 if len(argv) > 2 else 0.005
    e = math.log1p(equiv)

    data = _load(raw)
    print(f'# Equivalence analysis — {raw.name}')
    print(f'equivalence bound: +/-{math.expm1(e)*100:g}%  '
          f'(ALPHA={ALPHA} per TOST leg)')
    for phase in ('null', 'real'):
        if phase in data:
            _analyse(phase, data[phase], e)
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))
