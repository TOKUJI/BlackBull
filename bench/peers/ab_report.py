"""Turn an ab_commit.sh raw.tsv into a report.

Standalone so a finished run can be re-analysed without re-measuring:

    uv run python bench/peers/ab_report.py bench/results/ab-commit-<ts>Z/raw.tsv

Why the mean and not the median
-------------------------------
The median is the usual choice for benchmark noise because it shrugs off
outliers.  It is the wrong choice here.  Restarting the server between runs
makes each run draw afresh from a distribution that is **bimodal** on this
class of box — the process lands in a fast or a slow state and stays there for
its whole run, roughly 15 % apart.  Against a two-mode sample the median does
not estimate a centre, it reports *which mode won a coin toss*: an 8-run arm
that lands 5/8 fast and one that lands 3/8 fast differ by the full mode gap,
so the median Δ swings ±12 % with the code held byte-identical.  The mean is
the estimator that converges, and its standard error is honest about how
slowly.

The `hi-mode` column is the diagnostic: when it is neither 0/n nor n/n the
sample is mixed, and any median-based Δ from that run should be discarded.
"""
from __future__ import annotations

import statistics as st
import sys
from pathlib import Path


def _se(xs: list[float]) -> float:
    return st.stdev(xs) / len(xs) ** 0.5 if len(xs) > 1 else float('nan')


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
    """Midpoint of the observed range — enough to label the two clusters when
    they are as far apart as they are here.  Not a clustering algorithm; it
    only has to answer 'is this sample mixed'."""
    return (min(vals) + max(vals)) / 2


def _delta(b: list[float], t: list[float]) -> tuple[float, float, float]:
    """Return (mean Δ %, 1-SE of that Δ %, median Δ %)."""
    mb, mt = st.mean(b), st.mean(t)
    d = (mt - mb) / mb * 100.0
    # First-order propagation of the two relative standard errors.
    sed = ((_se(b) / mb) ** 2 + (_se(t) / mt) ** 2) ** 0.5 * 100.0
    dmed = (st.median(t) - st.median(b)) / st.median(b) * 100.0
    return d, sed, dmed


def render(raw: Path) -> str:
    data = _load(raw)
    L: list[str] = []
    w = L.append

    w('| Phase | Arm | n | mean req/s | 1 SE | median | hi-mode | µs/req |')
    w('|---|---|---:|---:|---:|---:|---:|---:|')
    mixed = False
    for phase in ('null', 'real'):
        if phase not in data:
            continue
        allv = [x for arm in data[phase].values() for x in arm]
        thr = _split_point(allv)
        for arm in ('base', 'treat'):
            xs = data[phase].get(arm)
            if not xs:
                continue
            hi = sum(1 for x in xs if x > thr)
            if 0 < hi < len(xs):
                mixed = True
            w('| %s | %s | %d | %.0f | %.0f | %.0f | %d/%d | %.2f |'
              % (phase, arm, len(xs), st.mean(xs), _se(xs), st.median(xs),
                 hi, len(xs), 1e6 / st.mean(xs)))
    w('')

    summary: dict[str, tuple[float, float, float]] = {}
    for phase in ('null', 'real'):
        if phase in data and data[phase].get('base') and data[phase].get('treat'):
            summary[phase] = _delta(data[phase]['base'], data[phase]['treat'])

    if 'null' in summary:
        d, sed, dmed = summary['null']
        w('**Null (A/A) Δ = %+.2f %% ± %.2f** (1 SE).  Both arms ran identical '
          'bytes, so the true value is 0 — this is the method\'s own bias, '
          'measured, not assumed.  Median Δ for the same data: %+.2f %%.'
          % (d, sed, dmed))
        w('')
    if 'real' in summary:
        d, sed, dmed = summary['real']
        w('**Real Δ = %+.2f %% ± %.2f** (1 SE).  Median Δ: %+.2f %%.'
          % (d, sed, dmed))
        w('')

    if mixed:
        w('> **Bimodal sample.** At least one arm split across both modes '
          '(`hi-mode` neither 0/n nor n/n), so its median is a coin toss '
          'between two clusters ~15 % apart, not a centre. Read the mean '
          'column; the median is shown only to make the trap visible.')
        w('')

    if 'real' in summary:
        d, sed, _ = summary['real']
        floor = abs(summary['null'][0]) + summary['null'][1] if 'null' in summary else 0.0
        if abs(d) <= sed:
            w('|Δ| = %.2f %% is within its own 1 SE (%.2f %%) — **consistent '
              'with no change**.' % (abs(d), sed))
        elif abs(d) <= floor:
            w('|Δ| = %.2f %% does not clear the null control\'s |bias| + SE '
              '(%.2f %%) — **this box cannot resolve the change**.'
              % (abs(d), floor))
        else:
            w('|Δ| = %.2f %% clears both its own SE (%.2f %%) and the null '
              'floor (%.2f %%). Confirm on a quiet host before believing it.'
              % (abs(d), sed, floor))
        w('')
        n = len(data['real']['base'])
        w('Resolution note: %d runs/arm at this spread bounds the effect at '
          'roughly ±%.1f %% (1 SE). A change believed smaller than that needs '
          'either many more rounds or a quieter host — `bench/aws/full_ab.sh` '
          'with `BASE_REF` is the harness for that.' % (n, sed))
    w('')
    w('## Raw')
    w('')
    w('| phase | round | arm | req/s |')
    w('|---|---|---|---:|')
    for line in raw.read_text().splitlines()[1:]:
        p = line.split('\t')
        if len(p) >= 4:
            w('| %s | %s | %s | %s |' % (p[0], p[1], p[2], p[3]))
    return '\n'.join(L) + '\n'


if __name__ == '__main__':
    if len(sys.argv) < 2:
        print(__doc__)
        raise SystemExit(2)
    print(render(Path(sys.argv[1])), end='')
