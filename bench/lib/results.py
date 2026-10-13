"""Run directories for bench tools: ``bench/results/<tool>/<UTC>-<rev>/``.

A run directory holds the tool's output, ``manifest.json`` recording what
produced it, and ``tmp/`` for the run's temporary files.  ``BENCH_RUN_DIR``
overrides the location: a tool started by another tool joins its caller's run,
or gets the fresh directory its caller named for it.

Shell tools source ``results.sh``; it calls this file as a script::

    python3 bench/lib/results.py <tool> [key=value ...] [-- argv ...]
    python3 bench/lib/results.py --latest <tool>
"""
from __future__ import annotations

import itertools
import json
import os
import platform
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]


def results_root() -> Path:
    return Path(os.environ.get('BENCH_RESULTS_ROOT') or REPO / 'bench' / 'results')


def _git(*args: str) -> str:
    try:
        return subprocess.run(['git', '-C', str(REPO), *args], capture_output=True,
                              text=True, timeout=10, check=True).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ''


def _sequence(run: Path) -> tuple[str, int]:
    """``<stamp>-<rev>[-<n>]`` → (stamp, n): creation order."""
    stamp, _, n = (run.name.split('-') + ['1'])[:3]
    return stamp, int(n)


def latest_run(tool: str) -> Path | None:
    runs = [p for p in (results_root() / tool).glob('*') if p.is_dir()]
    return max(runs, key=_sequence, default=None)


def run_dir(tool: str, settings: dict | None = None, argv: list[str] | None = None) -> Path:
    """Create the run directory for *tool*, or return the one BENCH_RUN_DIR names."""
    revision = _git('rev-parse', 'HEAD')
    if named := os.environ.get('BENCH_RUN_DIR'):
        run = Path(named)
        if run.exists():
            return run
        run.mkdir(parents=True)
    else:
        stamp = time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())
        base = results_root() / tool / f'{stamp}-{revision[:8] or "norev"}'
        base.parent.mkdir(parents=True, exist_ok=True)
        for n in itertools.count(1):
            run = base if n == 1 else base.with_name(f'{base.name}-{n}')
            try:
                run.mkdir()
                break
            except FileExistsError:
                continue
    (run / 'tmp').mkdir()
    (run / 'manifest.json').write_text(json.dumps({
        'tool': tool,
        'started': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
        'revision': revision,
        'dirty': bool(_git('status', '--porcelain', '--untracked-files=no')),
        'host': platform.node(),
        'argv': argv if argv is not None else sys.argv,
        'settings': settings or {},
    }, indent=2) + '\n')
    return run


def _main(args: list[str]) -> None:
    if args[:1] == ['--latest']:
        print(latest_run(args[1]) or '')
        return
    split = args.index('--') if '--' in args else len(args)
    tool, *pairs = args[:split]
    print(run_dir(tool, dict(p.split('=', 1) for p in pairs), args[split + 1:] or None))


if __name__ == '__main__':
    _main(sys.argv[1:])
