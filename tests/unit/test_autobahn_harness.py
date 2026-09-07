"""Exercise CI verdicts and evidence retention without Docker or a server."""

import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys

import pytest


SCRIPTS = Path(__file__).parents[2] / 'bench/conformance'
PASS = {'BlackBull': {'13.3.10': {'behavior': 'OK', 'behaviorClose': 'OK'}}}


def _script(path, body):
    path.write_text('#!/usr/bin/env bash\nset -eu\n' + body)
    path.chmod(0o755)


@pytest.fixture
def harness(tmp_path):
    scripts = tmp_path / 'bench/conformance'
    scripts.mkdir(parents=True)
    for name in ('autobahn_run.sh', 'autobahn_heavy.sh', 'autobahn_assert.sh',
                 'autobahn_fuzzingclient.json'):
        shutil.copyfile(SCRIPTS / name, scripts / name)
    bindir = tmp_path / 'bin'
    bindir.mkdir()
    _script(bindir / 'sleep', 'exit 0\n')
    _script(bindir / 'date', 'echo 20000101-000000\n')
    _script(bindir / 'python3',
            'if [[ "$1" == -c ]]; then exit 0; fi\n'
            f'exec {shlex.quote(sys.executable)} "$@"\n')
    env = {**os.environ, 'PATH': f'{bindir}:{os.environ["PATH"]}',
           'CASES': '13.3.10', 'MAX_ATTEMPTS': '1', 'TMPDIR': str(tmp_path)}
    env.pop('EXCLUDE_CASES', None)
    return tmp_path, scripts, bindir, env


def _run(harness, name):
    root, scripts, _, env = harness
    return subprocess.run(['bash', str(scripts / name)], cwd=root, env=env,
                          capture_output=True, text=True, timeout=10)


@pytest.mark.parametrize(('report', 'exit_code', 'accepted'), [
    (PASS, 0, True),
    (PASS, 137, False),
    ({'BlackBull': {}}, 0, False),
    ({}, 0, False),
    (None, 0, False),
    ({'BlackBull': {'13.3.10': {'behavior': 'FAILED', 'behaviorClose': 'OK'}}}, 0, False),
])
def test_heavy_requires_successful_process_and_nonempty_passing_report(
    harness, report, exit_code, accepted,
):
    root, scripts, _, _ = harness
    result = root / 'result'
    result.mkdir()
    if report is not None:
        (result / 'index.json').write_text(json.dumps(report))
    _script(scripts / 'autobahn_run.sh',
            f'echo "Results: {result}"\nexit {exit_code}\n')
    completed = _run(harness, 'autobahn_heavy.sh')
    assert (completed.returncode == 0) is accepted, completed.stdout + completed.stderr


def test_heavy_retry_uses_its_own_report(harness):
    root, scripts, _, env = harness
    env['MAX_ATTEMPTS'] = '2'
    (root / 'first').mkdir()
    (root / 'first/index.json').write_text(json.dumps(PASS))
    _script(scripts / 'autobahn_run.sh',
            'if [[ ! -f attempted ]]; then\n'
            '  touch attempted\n  echo "Results: first"\n  exit 137\n'
            'fi\necho "Results: missing-second"\nexit 0\n')
    completed = _run(harness, 'autobahn_heavy.sh')
    assert completed.returncode != 0
    assert 'attempt 2/2' in completed.stdout


def test_heavy_accepts_a_successful_retry(harness):
    root, scripts, _, env = harness
    env['MAX_ATTEMPTS'] = '2'
    (root / 'second').mkdir()
    (root / 'second/index.json').write_text(json.dumps(PASS))
    _script(scripts / 'autobahn_run.sh',
            'if [[ ! -f attempted ]]; then\n'
            '  touch attempted\n  echo "Results: missing-first"\n  exit 137\n'
            'fi\necho "Results: second"\nexit 0\n')
    completed = _run(harness, 'autobahn_heavy.sh')
    assert completed.returncode == 0, completed.stderr
    assert 'passed on attempt 2' in completed.stdout


@pytest.mark.parametrize(('behavior', 'close'), [
    ('OK', 'OK'), ('NON-STRICT', 'OK'), ('INFORMATIONAL', 'INFORMATIONAL'),
])
def test_assert_preserves_accepted_case_verdicts(harness, behavior, close):
    root, _, _, _ = harness
    result = root / 'bench/conformance/results/autobahn_test'
    result.mkdir(parents=True)
    (result / 'index.json').write_text(json.dumps({
        'BlackBull': {'13.3.10': {'behavior': behavior, 'behaviorClose': close}},
    }))
    assert _run(harness, 'autobahn_assert.sh').returncode == 0


def _docker(harness, *, exit_code=0, created=True, interrupt=False):
    root, _, bindir, env = harness
    env.update({'FAKE_EXIT': str(exit_code), 'FAKE_CREATED': str(int(created)),
                'FAKE_INTERRUPT': str(int(interrupt)), 'FAKE_ROOT': str(root)})
    (root / 'fake-report.json').write_text(json.dumps(PASS))
    _script(bindir / 'docker', r'''
echo "$*" >> "$FAKE_ROOT/docker-calls"
case "$1" in
  run)
    shift
    cidfile=""
    out=""
    while (( $# )); do
      case "$1" in
        --cidfile) cidfile="$2"; shift ;;
        --cidfile=*) cidfile="${1#*=}" ;;
        -v) shift; [[ "$1" != *:/results ]] || out="${1%:/results}" ;;
      esac
      shift
    done
    if [[ "$FAKE_CREATED" == 1 ]]; then
      [[ -z "$cidfile" ]] || echo fake-container > "$cidfile"
      cp "$FAKE_ROOT/fake-report.json" "$out/index.json"
    fi
    echo 'Running test case ID 13.3.10'
    echo 'tester diagnostic' >&2
    if [[ "$FAKE_INTERRUPT" == 1 ]]; then kill -TERM "$PPID"; fi
    exit "$FAKE_EXIT"
    ;;
  inspect)
    printf '{"ExitCode":%s,"OOMKilled":%s,"Running":false}\n' \
      "$FAKE_EXIT" "$([[ "$FAKE_EXIT" == 137 ]] && echo true || echo false)"
    ;;
  rm) echo removed >> "$FAKE_ROOT/cleanup" ;;
  *) exit 2 ;;
esac
''')


@pytest.mark.parametrize(('exit_code', 'created'), [(0, True), (137, True),
                                                      (139, True), (125, False)])
def test_run_preserves_status_logs_and_container_state(harness, exit_code, created):
    root, _, _, _ = harness
    _docker(harness, exit_code=exit_code, created=created)
    completed = _run(harness, 'autobahn_run.sh')
    assert completed.returncode == exit_code, completed.stderr
    result, = (root / 'bench/conformance/results').iterdir()
    assert (result / 'exit-code.txt').read_text().strip() == str(exit_code)
    log = (result / 'tester.log').read_text()
    assert 'Running test case ID 13.3.10' in log
    assert 'tester diagnostic' in log
    if created:
        state = json.loads((result / 'container-state.json').read_text())
        assert state['ExitCode'] == exit_code
        assert state['OOMKilled'] is (exit_code == 137)
        assert (root / 'cleanup').read_text() == 'removed\n'
    else:
        assert not (root / 'cleanup').exists()
    calls = (root / 'docker-calls').read_text()
    assert 'PYTHONUNBUFFERED=1' in calls
    config = json.loads((result / 'fuzzingclient.json').read_text())
    assert config['cases'] == ['13.3.10']


def test_run_directories_cannot_reuse_a_same_second_report(harness):
    root, _, _, _ = harness
    _docker(harness)
    assert _run(harness, 'autobahn_run.sh').returncode == 0
    assert _run(harness, 'autobahn_run.sh').returncode == 0
    results = list((root / 'bench/conformance/results').iterdir())
    assert len(results) == 2


def test_run_cleans_up_its_container_on_term(harness):
    root, _, _, _ = harness
    _docker(harness, exit_code=143, interrupt=True)
    completed = _run(harness, 'autobahn_run.sh')
    assert completed.returncode == 143
    assert (root / 'cleanup').read_text() == 'removed\n'
    result, = (root / 'bench/conformance/results').iterdir()
    assert (result / 'exit-code.txt').read_text().strip() == '143'
