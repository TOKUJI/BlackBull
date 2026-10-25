import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[2]
JOB = ROOT / 'bench/cloud/jobs/client-ab.sh'
PAYLOAD = ('run.py', 'driver.py', 'probe.py', 'proof.inc', 'monitor.py',
           'report.py', 'estimator.py', 'sources.json')


@pytest.fixture
def payload(tmp_path):
    helpers = tmp_path / 'bench/lib'
    helpers.mkdir(parents=True)
    shutil.copy2(ROOT / 'bench/lib/results.py', helpers / 'results.py')
    run = tmp_path / 'bench/results/client-input/run'
    run.mkdir(parents=True)
    for name in PAYLOAD:
        (run / name).write_text('')
    sources = {}
    for arm in ('base', 'treat'):
        path = run / arm / 'blackbull/x.py'
        path.parent.mkdir(parents=True)
        path.write_text(f'ARM = {arm!r}\n')
        sources[arm] = {'blackbull/x.py': hashlib.sha256(path.read_bytes()).hexdigest()}
    (run / 'sources.json').write_text(json.dumps(sources))
    working = tmp_path / 'blackbull/x.py'
    working.parent.mkdir()
    working.write_bytes((run / 'treat/blackbull/x.py').read_bytes())
    return tmp_path, run


def preflight(payload, **environment):
    repo, run = payload
    return subprocess.run(['bash', '-c',
        'source "$JOB" && job_preflight && job_runner'],
        env={**os.environ, 'JOB': str(JOB), 'AB_CLIENT_RUN': str(run),
             'REPO_ROOT': str(repo), 'REMOTE_REPO': '/home/ubuntu/BlackBull',
             'REMOTE_RUN': '/home/ubuntu/BlackBull/bench/results/cloud_ab/run',
             **environment},
        text=True, capture_output=True, timeout=10)


def test_client_job_runs_the_frozen_probe(payload):
    result = preflight(payload)
    assert result.returncode == 0, result.stderr
    assert '.venv-client-ab/bin/python' in result.stdout
    assert '/run.py' in result.stdout
    assert 'ab_commit.sh' not in result.stdout


def test_client_job_refuses_modified_snapshot(payload):
    repo, run = payload
    (run / 'treat/blackbull/x.py').write_text('CHANGED = True\n')
    result = preflight(payload)
    assert result.returncode != 0
    assert 'snapshot' in result.stderr


def test_client_job_refuses_unmeasured_working_code(payload):
    repo, _ = payload
    (repo / 'blackbull/x.py').write_text('CHANGED = True\n')
    result = preflight(payload)
    assert result.returncode != 0
    assert 'working' in result.stderr


def test_client_job_refuses_missing_runner(payload):
    _, run = payload
    (run / 'probe.py').unlink()
    result = preflight(payload)
    assert result.returncode != 0


def test_client_job_uses_configured_results_root(payload):
    repo, run = payload
    root = repo / 'custom-results'
    root.mkdir()
    moved = Path(shutil.move(str(run), root / 'run'))
    result = preflight((repo, moved), BENCH_RESULTS_ROOT=str(root))
    assert result.returncode == 0, result.stderr


def test_client_job_refuses_input_outside_results_root(payload):
    repo, run = payload
    moved = Path(shutil.move(str(run), repo / 'outside'))
    result = preflight((repo, moved))
    assert result.returncode != 0
    assert 'input must be a bench run directory' in result.stderr


@pytest.mark.parametrize('client', [False, True])
def test_wrapper_selects_the_requested_job(tmp_path, client):
    scripts = tmp_path / 'scripts'
    cloud = tmp_path / 'bench/cloud'
    scripts.mkdir()
    cloud.mkdir(parents=True)
    shutil.copy2(ROOT / 'scripts/run-ab-verify.sh', scripts / 'run-ab-verify.sh')
    (cloud / 'session.sh').write_text('printf "%s\\n" "$1"\n')
    env = {**os.environ, 'APPROVE_CLOUD': '1'}
    env.pop('AB_CLIENT_RUN', None)
    if client:
        env['AB_CLIENT_RUN'] = '/reviewed/input'
    result = subprocess.run(['bash', str(scripts / 'run-ab-verify.sh')],
                            env=env, text=True, capture_output=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == ('client-ab' if client else 'ab')
