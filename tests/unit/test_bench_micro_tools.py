"""The micro instruments run, check themselves, and keep their numbers in a run directory."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

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


@pytest.mark.skipif(shutil.which('openssl') is None or len(os.sched_getaffinity(0)) < 2,
                    reason='wire client probe needs openssl and two CPUs')
@pytest.mark.skipif(sys.version_info < (3, 12), reason='sys.monitoring needs Python 3.12+')
@pytest.mark.timeout(90)
@pytest.mark.no_audit(reason='The benchmark subprocesses use their own instruction monitoring and timing; inherited tracing changes their measurements and deadlines.')
def test_websocket_client_probe_checks_both_transports_and_source_controls(tmp_path):
    cpus = sorted(os.sched_getaffinity(0))
    done = _run(tmp_path, 'bench/micro/websocket_client.py', '--base', str(ROOT),
                '--head', str(ROOT), '--rounds', '1', '--control-rounds', '1',
                '--messages', '2', '--connections', '1', '--gc', 'on',
                '--cases', 'text,close,http-churn',
                '--client-cpu', str(cpus[0]), '--peer-cpu', str(cpus[-1]))
    assert done.returncode == 0, done.stderr
    run, = (tmp_path / 'websocket_client').iterdir()
    samples = [json.loads(line) for line in (run / 'samples.jsonl').read_text().splitlines()]
    assert len(samples) == 12
    assert {row['phase'] for row in samples} == {'null', 'path', 'real'}
    expected = {f'{protocol}/{case}' for protocol in ('h1', 'h1tls', 'h2tls')
                for case in ('text', 'close', 'http-churn')}
    assert all(set(row['metrics']) == expected for row in samples)
    assert all(row['source_proof'] and row['pending_tasks'] == [] for row in samples)
    assert all(len({m['process_id'] for m in row['metrics'].values()}) == len(expected)
               for row in samples)
    instructions = json.loads((run / 'instructions.json').read_text())
    assert all(set(row['metrics']) == expected for row in instructions.values())
    assert all(row['metrics']['h2tls/text']['instructions'] for row in instructions.values())
    assert (run / 'summary.md').exists() and (run / 'COMPLETE').exists()
    assert len(next(run.glob('*-cpu_us.tsv')).read_text().splitlines()[1].split('\t')[4]) == 64


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


def test_wire_instrument_rejects_mislabeled_custom_status(tmp_path):
    done = _run(tmp_path, 'bench/ab/response_wire.py', '--app', 'unused.py',
                '--status', '201')
    assert done.returncode == 2
    assert '--app requires --status 200' in done.stderr
    assert not done.stdout


@pytest.mark.skipif(shutil.which('h2load') is None or not {3, 4} <= os.sched_getaffinity(0),
                    reason='wire benchmark needs h2load and CPUs 3 and 4')
@pytest.mark.parametrize('reject', [False, True])
def test_wire_instrument_checks_custom_app_before_measuring(tmp_path, reject):
    app = tmp_path / 'custom_app.py'
    app.write_text('''from blackbull import BlackBull, Response
app = BlackBull()
@app.route(path='/custom')
async def respond(conn):
    assert conn.headers.get(b'x-bench') == b'proof'
    return Response(b'proof')
def check_response(response):
    assert response.body == b'proof'
''' + ("    raise ValueError('deliberate readiness rejection')\n" if reject else ''))
    done = _run(tmp_path, 'bench/ab/response_wire.py', '--base', str(ROOT), '--head', str(ROOT),
                '--app', str(app), '--path', '/custom', '--header', 'x-bench:proof',
                '--rounds', '2', '--count', '1000', '--protocol', 'h1', '--asgi', '0')
    if reject:
        assert done.returncode != 0
        assert 'deliberate readiness rejection' in done.stderr
        assert not any('"arm"' in line for line in done.stdout.splitlines())
    else:
        assert done.returncode == 0, done.stderr
        samples = [json.loads(line) for line in done.stdout.splitlines()]
        assert samples[0]['metadata']['app']['path'] == str(app)
        assert samples[0]['metadata']['headers'] == ['x-bench:proof']
        assert len([sample for sample in samples if 'arm' in sample]) == 8
        assert samples[-1]['complete'] is True
