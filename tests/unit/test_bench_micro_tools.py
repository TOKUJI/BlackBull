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


def test_testing_client_instrument_records_control_and_import_proof(tmp_path):
    done = _run(tmp_path, 'bench/micro/testing_client.py', '--base', str(ROOT),
                '--head', str(ROOT), '--rounds', '2', '--count', '2')
    assert done.returncode == 0, done.stderr
    run, = (tmp_path / 'testing_client').iterdir()
    samples = [json.loads(line) for line in (run / 'samples.jsonl').read_text().splitlines()]
    assert len(samples) == 16
    assert {s['phase'] for s in samples} == {'null', 'real'}
    assert all(s['source'] == str(ROOT / 'blackbull/testing/__init__.py') for s in samples)
    assert all(s['rates']['http'] > 0 and s['rates']['ws_roundtrip'] > 0 for s in samples)
    bytecode = json.loads((run / 'bytecode.json').read_text())
    assert bytecode['changed_modules'] == []
    assert (run / 'http.tsv').exists()


def test_paired_report_keeps_all_rounds_when_rates_have_two_modes(tmp_path):
    from bench.ab.report import analyse
    raw = tmp_path / 'raw.tsv'
    raw.write_text('phase\tround\tarm\tvalue\n' + ''.join(
        f'{phase}\t{rnd}\t{arm}\t{value + rnd}\n'
        for phase in ('null', 'real') for rnd in range(4)
        for arm in ('base', 'treat') for value in (100, 200)))
    result = analyse(raw, 1.0, split_modes=False)
    assert all(p['method'] == 'paired' and p['rounds'] == 4
               and p['n'] == {'base': 8, 'treat': 8}
               for p in result['phases'].values())
