"""The micro instruments run, check themselves, and keep their numbers in a run directory."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _run(tmp_path: Path, *argv: str) -> subprocess.CompletedProcess[str]:
    env = {k: v for k, v in os.environ.items() if k != 'BENCH_RUN_DIR'}
    return subprocess.run([sys.executable, *argv], cwd=ROOT, capture_output=True, text=True,
                          timeout=120, env={**env, 'BENCH_RESULTS_ROOT': str(tmp_path)})


def test_the_parser_benchmark_validates_every_parser_before_timing(tmp_path):
    done = _run(tmp_path, 'bench/micro/parser_microbench.py', '--iters', '20', '--counts', '1', '4')
    assert done.returncode == 0, done.stderr

    assert 'gate: PASS' in done.stdout
    run, = (tmp_path / 'parser_microbench').iterdir()
    points = json.loads((run / 'points.json').read_text())
    assert set(points['us']['blackbull']) == {'1', '4'}
