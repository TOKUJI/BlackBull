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


def test_client_job_pins_install_smoke_python(payload):
    repo, run = payload
    result = subprocess.run(['bash', '-c',
        'source "$JOB" && job_preflight && printenv CLOUD_SMOKE_PYTHON_VERSION'],
        env={**os.environ, 'JOB': str(JOB), 'AB_CLIENT_RUN': str(run),
             'REPO_ROOT': str(repo)}, text=True, capture_output=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == '3.14.6'


@pytest.mark.parametrize('pinned,exit_code', [(False, 0), (True, 0), (True, 7)])
def test_install_smoke_uses_selected_environment_and_retains_failure(tmp_path, pinned, exit_code):
    installer = (ROOT / 'bench/cloud/install.sh').read_text()
    smoke = installer.split('echo "=== Smoke test ==="', 1)[1].split(
        'echo "=== Server CPU topology ==="', 1)[0]
    for name in ('.venv', '.venv-client-ab'):
        python = tmp_path / name / 'bin/python'
        python.parent.mkdir(parents=True)
        python.write_text('#!/bin/bash\nprintf "%s\\n" "$0 $*" >> commands\n'
                          'if [ "$1" = -m ]; then echo FULL_TRACE_DETAIL; exit "$PYTEST_RC"; fi\n')
        python.chmod(0o755)
    tools = tmp_path / 'tools'
    tools.mkdir()
    uv = tools / 'uv'
    uv.write_text('#!/bin/bash\nprintf "%s\\n" "$UV_PROJECT_ENVIRONMENT:$*" >> commands\n')
    uv.chmod(0o755)
    environment = {**os.environ, 'PATH': f'{tools}:{os.environ["PATH"]}',
                   'PYTEST_RC': str(exit_code)}
    environment.pop('CLOUD_SMOKE_PYTHON_VERSION', None)
    if pinned:
        environment['CLOUD_SMOKE_PYTHON_VERSION'] = '3.14.6'
    result = subprocess.run(['bash', '-euo', 'pipefail', '-c', smoke],
                            cwd=tmp_path, env=environment, text=True,
                            capture_output=True, timeout=10)
    assert result.returncode == exit_code, result.stderr
    commands = (tmp_path / 'commands').read_text()
    selected = '.venv-client-ab' if pinned else '.venv'
    assert f'{selected}/bin/python -m pytest' in commands
    assert ('sync --locked --python 3.14.6' in commands) is pinned
    if pinned:
        assert '.venv-client-ab:sync' in commands
        for extra in ('testing', 'speed', 'compression', 'profiling', 'fault-injection', 'reload'):
            assert f'--extra {extra}' in commands
    assert 'FULL_TRACE_DETAIL' in result.stdout
    assert 'FULL_TRACE_DETAIL' in (tmp_path / 'bench/.state/install-smoke.log').read_text()


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


def manifest_payload(payload):
    _, run = payload
    helper = run / 'bench/micro/tool.py'
    helper.parent.mkdir(parents=True)
    helper.write_text('print("frozen")\n')
    sources = json.loads((run / 'sources.json').read_text())
    sources['files'] = {name: hashlib.sha256((run / name).read_bytes()).hexdigest()
                        for name in ('run.py', 'bench/micro/tool.py')}
    (run / 'sources.json').write_text(json.dumps(sources))
    (run / 'probe.py').unlink()
    return run, helper


def test_client_job_accepts_hash_verified_nested_harness(payload):
    manifest_payload(payload)
    result = preflight(payload, AB_CLIENT_TIMEOUT_SECONDS='5400')
    assert result.returncode == 0, result.stderr
    assert 'timeout 5400 ' in result.stdout


def test_client_job_refuses_changed_harness(payload):
    _, helper = manifest_payload(payload)
    helper.write_text('changed\n')
    result = preflight(payload)
    assert result.returncode != 0
    assert 'harness' in result.stderr


def test_client_job_refuses_traversal_in_manifest(payload):
    run, _ = manifest_payload(payload)
    sources = json.loads((run / 'sources.json').read_text())
    sources['files']['../outside.py'] = '0' * 64
    (run / 'sources.json').write_text(json.dumps(sources))
    result = preflight(payload)
    assert result.returncode != 0
    assert 'path' in result.stderr


def test_client_job_refuses_output_marker_as_input(payload):
    run, _ = manifest_payload(payload)
    (run / 'COMPLETE').write_text('old result\n')
    sources = json.loads((run / 'sources.json').read_text())
    sources['files']['COMPLETE'] = hashlib.sha256((run / 'COMPLETE').read_bytes()).hexdigest()
    (run / 'sources.json').write_text(json.dumps(sources))
    result = preflight(payload)
    assert result.returncode != 0


@pytest.mark.parametrize('value', ['0', '-1', '999999', '1;exit 0'])
def test_client_job_refuses_invalid_timeout(payload, value):
    result = preflight(payload, AB_CLIENT_TIMEOUT_SECONDS=value)
    assert result.returncode != 0


def test_client_job_refuses_linked_harness_directory(payload):
    run, helper = manifest_payload(payload)
    content = helper.read_bytes()
    helper.unlink()
    helper.parent.rmdir()
    outside = run.parent / 'outside'
    outside.mkdir()
    (outside / helper.name).write_bytes(content)
    helper.parent.symlink_to(outside, target_is_directory=True)
    result = preflight(payload)
    assert result.returncode != 0
    assert 'linked' in result.stderr


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
