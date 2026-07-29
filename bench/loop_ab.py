#!/usr/bin/env python3
"""Two-arm event-loop A/B — the permanent form of the uvloop thermometer.

`BB_UVLOOP` is not a shipping strategy: pure Python is an identity, and the
default stays `0`.  What the uvloop arm is *for* is diagnosis.  A server that
barely touches the event loop gets nothing from uvloop (a bare
``asyncio.Protocol`` responder measures -0.7%); BlackBull's delta is therefore
a measurement of its own per-request loop exposure.  Driving that delta to
zero is the goal, and it needs an instrument that runs the same way every
time.

Both arms run the **same build**, in one session, on pinned disjoint cores,
with a discarded warm-up pass.  Only ``BB_UVLOOP`` differs.  A cross-session
difference is not an A/B and cannot be differenced to attribute a cause.

Output is a stock column, a uvloop column, and the gap — reported against the
*previous* run of this harness, so the ship/no-ship rule can be read straight
off it:

    BB_UVLOOP=0 (shipped) | BB_UVLOOP=1 (instrument) | verdict
    improves              | improves                 | MUST SHIP
    unchanged             | improves                 | MAY SHIP
    regresses             | improves                 | MUST NOT SHIP
    unchanged             | unchanged                | MUST NOT SHIP

Row 2 looks like a null result and is not: when the stock loop's own overhead
is large enough to mask a saving, the uvloop column is the only place that
saving is visible yet.  Row 3 is the hard veto — the shipped column must never
regress.

Usage::

    python bench/loop_ab.py                      # default sweep
    python bench/loop_ab.py --conns 64 --reps 5
    python bench/loop_ab.py --baseline bench/results/loop-ab-20260729-120000
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import socket
import statistics
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
RESULTS = REPO / 'bench' / 'results'

_RPS = re.compile(rb'Requests/sec:\s+([\d.]+)')

# Below this, a difference between two runs is noise on a pinned box, not a
# result.  Deliberately generous: the harness exists to catch tens of percent,
# and calling a 1% drift a regression would make the gate useless.
DEFAULT_NOISE_PCT = 2.0


def _wait_for_port(port: int, timeout: float = 20.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            socket.create_connection(('127.0.0.1', port), 0.3).close()
            return True
        except OSError:
            time.sleep(0.2)
    return False


def _kill_port(port: int) -> None:
    """Free the port by port, never by process-name pattern.

    A ``pkill -f`` on the server's command line also matches the harness
    process that spawned it.
    """
    subprocess.run(['fuser', '-k', f'{port}/tcp'],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(0.6)


# ``--repo`` cannot be honoured with ``PYTHONPATH``: an editable install
# registers a meta-path finder, and ``sys.meta_path`` is consulted before
# ``sys.path``, so the working tree wins no matter what the path says.  Drop
# the finder first, then prepend the requested checkout.  Without this the
# "baseline" arm silently measures the build under test — a harness that
# always reports no change.
_BOOTSTRAP = """
import sys, runpy
sys.meta_path[:] = [f for f in sys.meta_path
                    if '__editable__' not in getattr(f, '__module__', '')]
sys.path.insert(0, {repo!r})
runpy.run_path({app!r}, run_name='__main__')
"""


def _start_server(arm: int, port: int, srv_cpus: str, log: Path, repo: Path):
    env = dict(os.environ, BB_UVLOOP=str(arm), BB_ACCESS_LOG='0')
    boot = _BOOTSTRAP.format(repo=str(repo), app=str(repo / 'bench' / 'app.py'))
    cmd = ['taskset', '-c', srv_cpus, sys.executable, '-c', boot,
           '--no-tls', '--port', str(port), '--workers', '1']
    handle = log.open('wb')
    proc = subprocess.Popen(cmd, cwd=repo, env=env,
                            stdout=handle, stderr=subprocess.STDOUT)
    return proc, handle


def _resolve_build(repo: Path) -> str:
    """Report which ``blackbull`` the server process will actually import.

    Recorded in ``summary.json`` and checked against ``--repo`` before any
    measurement runs, because the failure mode this guards against — both
    arms importing the same tree — produces a plausible-looking table that
    says nothing.
    """
    boot = (_BOOTSTRAP.split('runpy.run_path')[0]
            + 'import blackbull; print(blackbull.__file__)')
    out = subprocess.run([sys.executable, '-c', boot.format(repo=str(repo))],
                         capture_output=True, text=True, cwd=repo)
    if out.returncode != 0:
        raise RuntimeError(f'could not import blackbull from {repo}:\n'
                           f'{out.stderr}')
    return out.stdout.strip()


def _wrk(url: str, gen_cpus: str, threads: int, conns: int,
         duration: int) -> tuple[float, bytes]:
    out = subprocess.run(
        ['taskset', '-c', gen_cpus, 'wrk', f'-t{threads}', f'-c{conns}',
         f'-d{duration}s', url],
        capture_output=True).stdout
    match = _RPS.search(out)
    return (float(match.group(1)) if match else 0.0), out


def measure(arm: int, conns: int, args, outdir: Path) -> float:
    """One arm, one connection count: warm up, then measure.  Returns req/s."""
    _kill_port(args.port)
    log = outdir / f'server-uvloop{arm}-c{conns}.log'
    proc, handle = _start_server(arm, args.port, args.srv_cpus, log,
                                 Path(args.repo).resolve())
    try:
        if not _wait_for_port(args.port):
            raise RuntimeError(f'server (BB_UVLOOP={arm}) never bound '
                               f'port {args.port} — see {log}')
        url = f'http://127.0.0.1:{args.port}{args.path}'
        # Discarded pass: import-time work, dict resizing, route-plan pinning.
        _wrk(url, args.gen_cpus, args.threads, conns, 3)
        rps, raw = _wrk(url, args.gen_cpus, args.threads, conns, args.duration)
        with (outdir / f'wrk-uvloop{arm}-c{conns}.txt').open('ab') as fh:
            fh.write(raw)
        return rps
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        handle.close()
        _kill_port(args.port)


def _classify(delta_pct: float, noise: float) -> str:
    if delta_pct > noise:
        return 'improves'
    if delta_pct < -noise:
        return 'regresses'
    return 'unchanged'


_VERDICTS = {
    ('improves', 'improves'): 'MUST SHIP',
    ('improves', 'unchanged'): 'SHIP — explain the instrument disagreeing',
    ('improves', 'regresses'): 'SHIP — explain the instrument disagreeing',
    ('unchanged', 'improves'): 'MAY SHIP — real removal, still masked on stock',
    ('unchanged', 'unchanged'): 'MUST NOT SHIP — improves nothing',
    ('unchanged', 'regresses'): 'MUST NOT SHIP — improves nothing',
    ('regresses', 'improves'): 'MUST NOT SHIP — bought with the shipped column',
    ('regresses', 'unchanged'): 'MUST NOT SHIP — shipped column regressed',
    ('regresses', 'regresses'): 'MUST NOT SHIP — shipped column regressed',
}


def _find_baseline(explicit: str | None, current: Path) -> Path | None:
    if explicit:
        return Path(explicit)
    runs = sorted(p for p in RESULTS.glob('loop-ab-*')
                  if p.is_dir() and p != current
                  and (p / 'summary.json').exists())
    return runs[-1] if runs else None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--conns', type=int, nargs='+', default=[8, 64, 256, 1024])
    ap.add_argument('--reps', type=int, default=3)
    ap.add_argument('--duration', type=int, default=10)
    ap.add_argument('--threads', type=int, default=4)
    ap.add_argument('--port', type=int, default=8899)
    ap.add_argument('--path', default='/ping')
    ap.add_argument('--srv-cpus', default=os.environ.get('SRV_CPUS', '0-1'))
    ap.add_argument('--gen-cpus', default=os.environ.get('GEN_CPUS', '6-11'))
    ap.add_argument('--noise-pct', type=float, default=DEFAULT_NOISE_PCT,
                    help='below this, a run-to-run difference is "unchanged"')
    ap.add_argument('--tag', default=None, help='result directory suffix')
    ap.add_argument('--repo', default=str(REPO),
                    help='checkout to serve from — point at a git worktree to '
                         'baseline a previous commit with this same harness')
    ap.add_argument('--baseline', default=None,
                    help='previous run dir to compare against '
                         '(default: most recent loop-ab-* in bench/results)')
    ap.add_argument('--report', default=None,
                    help='re-render the table for an existing run dir instead '
                         'of measuring — cheap, and never perturbs a box that '
                         'is mid-run')
    args = ap.parse_args()

    if args.report:
        cur_dir = Path(args.report)
        cur = json.loads((cur_dir / 'summary.json').read_text())
        cur.setdefault('tag', cur_dir.name)
        base_dir = _find_baseline(args.baseline, cur_dir)
        base = (json.loads((base_dir / 'summary.json').read_text())
                if base_dir else None)
        report(cur, base, base_dir, args)
        return 0

    for tool in ('wrk', 'taskset', 'fuser'):
        if shutil.which(tool) is None:
            print(f'error: {tool} not found on PATH', file=sys.stderr)
            return 2

    repo = Path(args.repo).resolve()
    build = _resolve_build(repo)
    if not build.startswith(str(repo) + os.sep):
        print(f'error: --repo is {repo} but the server would import '
              f'{build}\n       (an editable install elsewhere is winning)',
              file=sys.stderr)
        return 2
    print(f'serving from: {build}')

    stamp = datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')
    outdir = RESULTS / f'loop-ab-{args.tag or stamp}'
    outdir.mkdir(parents=True, exist_ok=True)

    samples: dict[str, dict[str, list[float]]] = {}
    for rep in range(1, args.reps + 1):
        for conns in args.conns:
            # Arms interleaved inside a rep so thermal / scheduler drift lands
            # on both columns rather than on whichever ran last.
            for arm in (0, 1):
                rps = measure(arm, conns, args, outdir)
                samples.setdefault(str(conns), {}).setdefault(str(arm), []).append(rps)
                print(f'rep={rep} conns={conns} BB_UVLOOP={arm} rps={rps:,.0f}',
                      flush=True)

    summary = {
        'stamp': stamp,
        'tag': outdir.name,
        'path': args.path,
        'duration': args.duration,
        'threads': args.threads,
        'reps': args.reps,
        'srv_cpus': args.srv_cpus,
        'gen_cpus': args.gen_cpus,
        'python': sys.version.split()[0],
        'repo': str(repo),
        'build': build,
        'samples': samples,
        'median': {c: {a: statistics.median(v) for a, v in arms.items()}
                   for c, arms in samples.items()},
    }
    (outdir / 'summary.json').write_text(json.dumps(summary, indent=2))

    base_dir = _find_baseline(args.baseline, outdir)
    base = (json.loads((base_dir / 'summary.json').read_text())
            if base_dir else None)
    report(summary, base, base_dir, args)
    return 0


def _spread_pct(values: list[float]) -> float:
    """Rep-to-rep range as a percentage of the median."""
    med = statistics.median(values)
    return (max(values) - min(values)) / med * 100 if med else 0.0


def report(summary, base, base_dir, args) -> None:
    conns_list = sorted((int(c) for c in summary['median']))
    print(f"\n=== {summary.get('tag', '')} — {summary['path']}, "
          f"{summary['reps']} reps, median req/s ===")
    header = (f'{"conns":>6}  {"BB_UVLOOP=0":>13} {"±":>5}  '
              f'{"BB_UVLOOP=1":>13} {"±":>5}  {"gap":>7}')
    if base:
        header += f'  {"Δstock":>8}  {"Δuvloop":>8}   verdict'
    print(header)

    signs = {'0': [], '1': []}
    deltas = {'0': [], '1': []}
    for conns in conns_list:
        key = str(conns)
        med = summary['median'][key]
        stock, uv = med['0'], med['1']
        sp_s = _spread_pct(summary['samples'][key]['0'])
        sp_u = _spread_pct(summary['samples'][key]['1'])
        gap = (uv / stock - 1) * 100 if stock else 0.0
        line = (f'{conns:>6}  {stock:>13,.0f} {sp_s:>4.0f}%  '
                f'{uv:>13,.0f} {sp_u:>4.0f}%  {gap:>6.1f}%')
        if base:
            prev = base['median'].get(key)
            if prev:
                d_stock = (stock / prev['0'] - 1) * 100 if prev['0'] else 0.0
                d_uv = (uv / prev['1'] - 1) * 100 if prev['1'] else 0.0
                for arm, delta in (('0', d_stock), ('1', d_uv)):
                    deltas[arm].append(delta)
                    signs[arm].append(1 if delta > 0 else -1)
                verdict = _VERDICTS[(_classify(d_stock, args.noise_pct),
                                     _classify(d_uv, args.noise_pct))]
                line += f'  {d_stock:>+7.1f}%  {d_uv:>+7.1f}%   {verdict}'
            else:
                line += f'  {"—":>8}  {"—":>8}   (not in baseline)'
        print(line)

    if not base:
        print('\nno previous run found — this run is the baseline')
        return

    # Per-cell verdicts are only as good as the box.  When the rep spread is
    # comparable to the delta, the cell says nothing on its own — but the
    # *sign* agreeing across independent connection levels does, because
    # scheduler and thermal noise have no reason to favour one direction at
    # every level at once.  Read this block, not a single row.
    worst = max(max(_spread_pct(summary['samples'][str(c)][a])
                    for a in ('0', '1')) for c in conns_list)
    print()
    for arm, label in (('0', 'BB_UVLOOP=0 (shipped)'),
                       ('1', 'BB_UVLOOP=1 (instrument)')):
        up = sum(1 for s in signs[arm] if s > 0)
        lo, hi = min(deltas[arm]), max(deltas[arm])
        agree = 'consistent' if up in (0, len(signs[arm])) else 'MIXED'
        print(f'  {label:<26} {up}/{len(signs[arm])} levels up  '
              f'({lo:+.1f}% .. {hi:+.1f}%)  {agree}')
    print(f'\nbaseline: {base_dir.name}  (noise band ±{args.noise_pct:.1f}%; '
          f'worst rep spread this run {worst:.0f}%)')
    if worst > args.noise_pct * 3:
        print('  NOTE: rep spread far exceeds the noise band — trust the '
              'sign-consistency lines above, not individual cells, and '
              'confirm on a quiet machine before quoting a number.')


if __name__ == '__main__':
    raise SystemExit(main())
