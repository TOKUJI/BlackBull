"""Verdict for an ABBA ``raw.tsv`` (phase, round, arm, value, proof).

    python bench/ab/report.py <raw.tsv> [--equiv PCT] [--json]

Deltas are log-scale (treat vs base) and reported as percentages.

* **Monomodal sample** (EC2): paired by round — each round's own Δ, so drift
  between rounds cancels — with a bootstrap 95 % CI over rounds.
* **Bimodal sample** (a box whose restarts land in a fast or a slow state,
  ~15 % apart): the median and the pooled mean measure which mode each arm
  happened to land in, so Δ is taken within each mode and combined by inverse
  variance, with a bootstrap 95 % CI.

The null (A/A) phase serves identical bytes under both labels; its CI is this
session's resolution floor — its bias as well as its width.  Verdict, on the
real phase:

* ``inconclusive`` fewer than MIN_ROUNDS rounds (a bootstrap over two or three
  rounds is only their range), or none of the below;
* ``unresolved``   the null CI does not fit inside ±equiv — this box cannot
  resolve a change of that size in this session;
* ``slower`` / ``faster``  the CI lies beyond 0 *and* beyond the null CI;
* ``equivalent``   otherwise, when the CI fits inside ±equiv.
"""
from __future__ import annotations

import argparse
import json
import math
import random
import statistics as st
from pathlib import Path

#: A gap this large (relative) between neighbouring sorted values splits a
#: phase into two modes; a monomodal sample's largest gap is a fraction of it.
MODE_GAP = 0.05
BOOTSTRAP = 4000
MIN_ROUNDS = 4
ARMS = ('base', 'treat')


def load(raw: Path) -> dict[str, list[tuple[int, str, float]]]:
    phases: dict[str, list[tuple[int, str, float]]] = {}
    for line in raw.read_text().splitlines()[1:]:
        parts = line.split('\t')
        if len(parts) < 4 or parts[2] not in ARMS:
            continue
        try:
            value = float(parts[3])
        except ValueError:
            continue
        if math.isfinite(value) and value > 0:
            phases.setdefault(parts[0], []).append((int(parts[1]), parts[2], value))
    return phases


def mode_split(values: list[float]) -> float | None:
    """The threshold between two modes, or None for a monomodal sample."""
    xs = sorted(values)
    best, at = 0.0, None
    for i in range(1, len(xs) - 1):
        gap = (xs[i] - xs[i - 1]) / xs[i - 1]
        if gap > best and i >= 2 and len(xs) - i >= 2:
            best, at = gap, (xs[i] + xs[i - 1]) / 2
    return at if best >= MODE_GAP else None


def _mean_log(xs: list[float]) -> float:
    return st.fmean(math.log(x) for x in xs)


def _paired(rows, rng):
    rounds: dict[int, dict[str, list[float]]] = {}
    for rnd, arm, v in rows:
        rounds.setdefault(rnd, {}).setdefault(arm, []).append(v)
    ds = [_mean_log(r['treat']) - _mean_log(r['base'])
          for r in rounds.values() if r.get('base') and r.get('treat')]
    if len(ds) < 2:
        return None
    boot = [st.fmean(rng.choices(ds, k=len(ds))) for _ in range(BOOTSTRAP)]
    return st.fmean(ds), boot, len(ds)


def _per_mode(rows, threshold, rng):
    groups = {(m, arm): [] for m in (False, True) for arm in ARMS}
    for _, arm, v in rows:
        groups[(v > threshold, arm)].append(v)
    modes = [m for m in (False, True)
             if len(groups[(m, 'base')]) >= 2 and len(groups[(m, 'treat')]) >= 2]
    if not modes:
        return None

    def estimate(pick):
        ds = {m: _mean_log(pick(groups[(m, 'treat')])) - _mean_log(pick(groups[(m, 'base')]))
              for m in modes}
        return sum(ds[m] * weights[m] for m in modes) / sum(weights.values())

    weights = {}
    for m in modes:
        var = sum(st.variance([math.log(x) for x in groups[(m, arm)]]) / len(groups[(m, arm)])
                  for arm in ARMS)
        weights[m] = 1 / max(var, 1e-12)
    boot = [estimate(lambda xs: rng.choices(xs, k=len(xs))) for _ in range(BOOTSTRAP)]
    return estimate(lambda xs: xs), boot, len({r for r, _, _ in rows})


def _pct(d: float) -> float:
    return math.expm1(d) * 100


def analyse(raw: Path, equiv: float, *, split_modes: bool = True) -> dict:
    rng = random.Random(0)
    phases = {}
    bimodal = False
    for name, rows in load(raw).items():
        arms = {arm: [v for _, a, v in rows if a == arm] for arm in ARMS}
        if not all(arms.values()):
            continue
        threshold = mode_split([v for _, _, v in rows]) if split_modes else None
        # A gap that separates the arms is the effect, not two modes: modes
        # exist only when each arm has runs on both sides of it.
        if threshold is not None and not all(
                min(xs) < threshold < max(xs) for xs in arms.values()):
            threshold = None
        result = _per_mode(rows, threshold, rng) if threshold is not None else None
        if result is None:
            threshold = None
            result = _paired(rows, rng)
        bimodal |= threshold is not None
        if result is None:
            continue
        d, boot, rounds = result
        boot.sort()
        phases[name] = {
            'delta_pct': _pct(d),
            'ci95': [_pct(boot[int(.025 * len(boot))]), _pct(boot[int(.975 * len(boot)) - 1])],
            'rounds': rounds,
            'method': 'per-mode' if threshold is not None else 'paired',
            'n': {arm: len(xs) for arm, xs in arms.items()},
            'mean': {arm: st.fmean(xs) for arm, xs in arms.items()},
            'hi_mode': ({arm: sum(x > threshold for x in xs) for arm, xs in arms.items()}
                        if threshold is not None else None),
        }
    return {'equiv_pct': equiv, 'bimodal': bimodal, 'phases': phases,
            'verdict': verdict(phases, equiv)}


def verdict(phases: dict, equiv: float) -> str:
    def inside(p):
        lo, hi = p['ci95']
        return -equiv < lo and hi < equiv

    real, null = phases.get('real'), phases.get('null')
    if real is None or real['rounds'] < MIN_ROUNDS:
        return 'inconclusive'
    if null is not None and not inside(null):
        return 'unresolved'
    lo, hi = real['ci95']
    null_lo, null_hi = null['ci95'] if null is not None else (0.0, 0.0)
    if hi < min(0.0, null_lo):
        return 'slower'
    if lo > max(0.0, null_hi):
        return 'faster'
    return 'equivalent' if inside(real) else 'inconclusive'


EXPLAIN = {
    'unresolved': 'the null (A/A) CI does not fit inside ±{e} %, so this session '
                  'cannot resolve a change of that size — add rounds or use a quieter host',
    'slower': 'the treatment is slower: the real CI lies below 0 and below the null CI',
    'faster': 'the treatment is faster: the real CI lies above 0 and above the null CI',
    'equivalent': 'no change beyond the null floor, and the real CI fits inside ±{e} %',
    'inconclusive': 'fewer than ' + str(MIN_ROUNDS) + ' rounds, or a real CI that neither '
                    'clears the null floor nor fits inside ±{e} % — add rounds',
}


def markdown(s: dict) -> str:
    out = ['| Phase | Arm | n | mean /s | µs per op | hi-mode |', '|---|---|---:|---:|---:|---:|']
    for name, p in s['phases'].items():
        for arm in ARMS:
            hi = f"{p['hi_mode'][arm]}/{p['n'][arm]}" if p['hi_mode'] else '—'
            out.append(f"| {name} | {arm} | {p['n'][arm]} | {p['mean'][arm]:.0f} | "
                       f"{1e6 / p['mean'][arm]:.2f} | {hi} |")
    out.append('')
    for name, p in s['phases'].items():
        lo, hi = p['ci95']
        how = ('paired by round' if p['method'] == 'paired'
               else 'within each mode, combined by inverse variance')
        out.append(f"**{name} Δ = {p['delta_pct']:+.2f} %** (95 % CI [{lo:+.2f}, {hi:+.2f}]), "
                   f"{how}, {p['rounds']} rounds.")
        out.append('')
    if s['bimodal']:
        out.append('> **Bimodal sample**: runs landed in two throughput modes; '
                   'medians and pooled means would compare the modes, not the code.')
        out.append('')
    out.append(f"**Verdict: {s['verdict']}** — "
               + EXPLAIN[s['verdict']].format(e=f"{s['equiv_pct']:g}") + '.')
    return '\n'.join(out) + '\n'


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    ap.add_argument('raw', type=Path)
    ap.add_argument('--equiv', type=float, default=1.0, help='equivalence bound, %%')
    ap.add_argument('--json', action='store_true')
    args = ap.parse_args()
    s = analyse(args.raw, args.equiv)
    print(json.dumps(s, indent=2) if args.json else markdown(s), end='\n' if args.json else '')


if __name__ == '__main__':
    main()
