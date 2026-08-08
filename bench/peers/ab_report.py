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

import math
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
            rps_f = float(rps)
        except ValueError:
            continue
        # A failed run (server not ready, load generator error) writes NaN.
        # Statistics cannot consume it, and reporting a mean built on it is
        # worse than reporting the missing run — skip non-finite rows so a
        # partial run still reports what did measure rather than crashing.
        if not math.isfinite(rps_f):
            continue
        out.setdefault(phase, {}).setdefault(arm, []).append(rps_f)
    return out


def _load_by_round(raw: Path) -> dict[str, dict[str, dict[str, list[float]]]]:
    """Same rows, keeping the round — ``phase -> round -> arm -> [req/s]``.

    The round is the blocking factor the ABBA schedule exists to create, and
    :func:`_load` drops it.  Keeping it is what makes the paired analysis below
    possible.
    """
    out: dict[str, dict[str, dict[str, list[float]]]] = {}
    for line in raw.read_text().splitlines()[1:]:
        parts = line.split('\t')
        if len(parts) < 4:
            continue
        phase, rnd, arm, rps = parts[:4]
        try:
            rps_f = float(rps)
        except ValueError:
            continue
        if not math.isfinite(rps_f):
            continue
        out.setdefault(phase, {}).setdefault(rnd, {}).setdefault(
            arm, []).append(rps_f)
    return out


def _paired_delta(by_round: dict[str, dict[str, list[float]]]):
    """Per-round Δ %, then the mean and SE *of those* — a paired analysis.

    The pooled ``_delta`` differences one grand mean against the other and
    propagates two independent standard errors, which charges the comparison
    for all the between-round drift the ABBA schedule was designed to cancel.
    Both arms run inside each round, so each round yields its own Δ and the
    drift subtracts out; the spread that remains is the spread of the Δ itself.

    On a box whose throughput wanders between rounds this is the difference
    between a decisive verdict and "consistent with no change".  It is not a
    way to make a small effect look real: if the per-round Δs disagree in sign,
    the SE reported here says so loudly.

    Returns ``(mean Δ %, 1 SE, n rounds, [per-round Δ %])`` or ``None`` when
    fewer than two rounds have both arms.
    """
    deltas: list[float] = []
    for rnd in sorted(by_round):
        arms = by_round[rnd]
        b, t = arms.get('base'), arms.get('treat')
        if not b or not t:
            continue
        mb, mt = st.mean(b), st.mean(t)
        if mb == 0:
            continue
        deltas.append((mt - mb) / mb * 100.0)
    if len(deltas) < 2:
        return None
    return st.mean(deltas), _se(deltas), len(deltas), deltas


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

    by_round = _load_by_round(raw)
    paired = {p: r for p in ('null', 'real')
              if (r := _paired_delta(by_round.get(p, {}))) is not None}
    #: ``(index, surviving deltas)`` once a round is found to be carrying the
    #: result — every verdict downstream reads this instead of the full mean.
    contaminated: tuple[int, list[float]] | None = None
    if paired:
        w('### Paired by round')
        w('')
        w('Both arms run inside every round, so each round carries its own Δ '
          'and the drift between rounds cancels instead of being charged to '
          'the comparison. The pooled figures above do not use that.')
        w('')
        w('| Phase | rounds | mean Δ % | 1 SE | per-round Δ % |')
        w('|---|---:|---:|---:|---|')
        for phase in ('null', 'real'):
            if phase in paired:
                d, sed, n, ds = paired[phase]
                w('| %s | %d | %+.2f | %.2f | %s |'
                  % (phase, n, d, sed,
                     ', '.join('%+.2f' % x for x in ds)))
        w('')
        if 'real' in paired:
            d, sed, n, ds = paired['real']
            floor = 0.0
            if 'null' in paired:
                nd, nse, _, _ = paired['null']
                floor = abs(nd) + nse
            # One round can carry the whole verdict when a single run lands in
            # the other throughput mode.  Say so rather than leaving it to be
            # spotted in the per-round column: leave-one-out is cheap, and a
            # mean that moves by more than its own SE when one round drops is
            # a mean describing that round.
            if len(ds) >= 4:
                # Is any one round an outlier *against the others* — not
                # "does the mean move", which a large SE hides by definition.
                # A single run landing in the other throughput mode shifts its
                # round's Δ by several percent while the rest sit within a
                # fraction of one, and that is what this catches.
                worst, dist, kept = None, 0.0, []
                for i in range(len(ds)):
                    rest = [x for j, x in enumerate(ds) if j != i]
                    spread = _se(rest)
                    if not math.isfinite(spread) or spread == 0:
                        continue
                    z = abs(ds[i] - st.mean(rest)) / spread
                    if z > dist:
                        worst, dist, kept = i, z, rest
                if worst is not None and dist > 5:
                    contaminated = (worst, kept)
                    w('> **One round carries the result.** Round %d (Δ %+.2f '
                      '%%) sits %.0f SE from the mean of the other rounds, '
                      'which give %+.2f %% ± %.2f between them. That is the '
                      'signature of a single run landing in the other '
                      'throughput mode, not of a %+.2f %% effect — quote the '
                      'remaining rounds, and say that you did.'
                      % (worst + 1, ds[worst], dist, st.mean(kept),
                         _se(kept), d))
                    w('')
            if contaminated is not None:
                # Every verdict below this point would be read off a mean the
                # paragraph above just disowned.  Re-derive them from the
                # rounds that survived instead: a report whose warning and
                # whose conclusion disagree gets quoted by its conclusion.
                _, kept = contaminated
                kd, ksed = st.mean(kept), _se(kept)
                same_sign = all(x > 0 for x in kept) or all(x < 0 for x in kept)
                if not same_sign:
                    w('Surviving rounds disagree in sign — no verdict.')
                elif abs(kd) > ksed and abs(kd) > floor:
                    w('**Verdict on the surviving rounds: Δ = %+.2f %% ± '
                      '%.2f**, clearing both its own SE and the paired null '
                      'floor (%.2f %%). The %+.2f %% pooled figure above is '
                      'not the result.' % (kd, ksed, floor, d))
                else:
                    w('**Verdict on the surviving rounds: Δ = %+.2f %% ± '
                      '%.2f**, which does not clear its SE or the paired null '
                      'floor (%.2f %%).' % (kd, ksed, floor))
            else:
                same_sign = all(x > 0 for x in ds) or all(x < 0 for x in ds)
                if not same_sign:
                    w('Per-round Δs disagree in sign — the effect is not '
                      'resolvable at this round count whatever the mean says.')
                elif abs(d) > sed and abs(d) > floor:
                    w('Paired Δ = %+.2f %% ± %.2f clears both its own SE and '
                      'the paired null floor (%.2f %%), and every round agrees '
                      'in sign.' % (d, sed, floor))
                else:
                    w('Paired Δ = %+.2f %% ± %.2f does not clear its SE '
                      '(%.2f %%) or the paired null floor (%.2f %%).'
                      % (d, sed, sed, floor))
        w('')

    if 'real' in summary:
        d, sed, _ = summary['real']
        floor = abs(summary['null'][0]) + summary['null'][1] if 'null' in summary else 0.0
        if contaminated is not None:
            # The pooled figures share the contaminated round, and the pooled
            # analysis cannot even see it — it has already thrown the rounds
            # away.  Say what it would have said and why it is not being said.
            w('Pooled verdict withheld: the pooled analysis differences two '
              'grand means and cannot exclude the round flagged above. Read '
              'the paired verdict.')
        elif 'real' in paired:
            # The paired verdict above is strictly better informed — it uses
            # the blocking factor this schedule was built to create.  Printing
            # a second, weaker verdict as its equal is how a report ends up
            # contradicting itself; label it as the sensitivity floor it is.
            w('For reference, the *unpaired* view of the same runs gives '
              '|Δ| = %.2f %% against a %.2f %% SE — it charges the comparison '
              'for between-round drift, so it is the weaker test and is not '
              'the verdict. Where the two disagree, the paired one is the one '
              'the design supports.' % (abs(d), sed))
        elif abs(d) <= sed:
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
