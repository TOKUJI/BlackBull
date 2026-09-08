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
                 'autobahn_fuzzingclient.json', 'autobahn_cases.py',
                 'autobahn_common.sh'):
        source = SCRIPTS / name
        if source.exists():
            shutil.copyfile(source, scripts / name)
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


def _heavy_case_ids():
    section_12 = [
        f'12.{group}.{case}'
        for group in range(1, 6)
        for case in (10, 14, 15, 16, 17, 18)
    ]
    section_13 = [
        f'13.{group}.{case}'
        for group in range(1, 8)
        for case in range(1, 19)
    ]
    return section_12 + section_13


def _cases_tool(tmp_path, *arguments):
    return subprocess.run(
        [sys.executable, str(SCRIPTS / 'autobahn_cases.py'), *map(str, arguments)],
        cwd=tmp_path, capture_output=True, text=True, timeout=10,
    )


def test_case_manifest_partitions_complete_resolved_coverage(tmp_path):
    resolved = tmp_path / 'resolved.json'
    manifest = tmp_path / 'manifest.json'
    resolved.write_text(json.dumps(_heavy_case_ids()))

    completed = _cases_tool(tmp_path, 'manifest', resolved, manifest)

    assert completed.returncode == 0, completed.stderr
    result = json.loads(manifest.read_text())
    batches = result['batches']
    assert [batch['name'] for batch in batches] == [
        '12.1', '12.2', '12.3', '12.4', '12.5',
        '13.1', '13.2', '13.3', '13.4', '13.5', '13.6', '13.7',
    ]
    flattened = [case_id for batch in batches for case_id in batch['cases']]
    assert flattened == _heavy_case_ids()
    assert len(flattened) == len(set(flattened)) == 156


@pytest.mark.parametrize('resolved_cases', [
    [],
    _heavy_case_ids()[:-1],
    _heavy_case_ids() + [_heavy_case_ids()[0]],
])
def test_case_manifest_rejects_empty_missing_or_duplicate_ids(
    tmp_path, resolved_cases,
):
    resolved = tmp_path / 'resolved.json'
    manifest = tmp_path / 'manifest.json'
    resolved.write_text(json.dumps(resolved_cases))

    completed = _cases_tool(tmp_path, 'manifest', resolved, manifest)

    assert completed.returncode != 0
    assert not manifest.exists()


@pytest.mark.parametrize('actual_cases', [
    _heavy_case_ids()[:5],
    [*_heavy_case_ids()[:5], '99.99.99'],
])
def test_case_report_rejects_partial_or_different_ids(tmp_path, actual_cases):
    expected = tmp_path / 'expected.json'
    report = tmp_path / 'index.json'
    expected.write_text(json.dumps(_heavy_case_ids()[:6]))
    report.write_text(json.dumps({
        'BlackBull': {
            case_id: {'behavior': 'OK', 'behaviorClose': 'OK'}
            for case_id in actual_cases
        },
    }))

    completed = _cases_tool(tmp_path, 'verify', expected, report)

    assert completed.returncode != 0


def _resolver(
    harness, *, exit_code=0, interrupt=False, remove_exit=0,
    create_exit=0, create_interrupt=False, cid='fake-resolver',
):
    root, _, bindir, env = harness
    resolved = root / 'fake-resolved.json'
    resolved.write_text(json.dumps(_heavy_case_ids()))
    env.update({
        'FAKE_RESOLVER_EXIT': str(exit_code),
        'FAKE_RESOLVER_INTERRUPT': str(int(interrupt)),
        'FAKE_CREATE_EXIT': str(create_exit),
        'FAKE_CREATE_INTERRUPT': str(int(create_interrupt)),
        'FAKE_RESOLVER_CID': cid,
        'FAKE_REMOVE_EXIT': str(remove_exit),
        'FAKE_RESOLVED': str(resolved),
        'FAKE_ROOT': str(root),
    })
    _script(bindir / 'docker', r'''
case "$1" in
  create)
    shift
    while (( $# )); do
      if [[ "$1" == --cidfile ]]; then
        printf '%s' "$FAKE_RESOLVER_CID" > "$2"
        break
      fi
      shift
    done
    if [[ "$FAKE_CREATE_INTERRUPT" == 1 ]]; then kill -TERM "$PPID"; fi
    exit "$FAKE_CREATE_EXIT"
    ;;
  cp)
    shift
    if [[ "$1" == *:/tmp/resolved.json ]]; then
      cp "$FAKE_RESOLVED" "$2"
    fi
    ;;
  start)
    if [[ "$FAKE_RESOLVER_INTERRUPT" == 1 ]]; then kill -TERM "$PPID"; fi
    exit "$FAKE_RESOLVER_EXIT"
    ;;
  inspect)
    printf '{"ExitCode":%s,"OOMKilled":false,"Running":false}\n' \
      "$FAKE_RESOLVER_EXIT"
    ;;
  rm)
    echo resolver-removed >> "$FAKE_ROOT/resolver-cleanup"
    exit "$FAKE_REMOVE_EXIT"
    ;;
  *) exit 2 ;;
esac
''')


def _batch_runner(harness, mode):
    root, scripts, _, env = harness
    runner = root / 'fake-batch-runner.py'
    runner.write_text('''
import json
import os
from pathlib import Path
import sys

root = Path(os.environ['FAKE_ROOT'])
counter = root / 'runner-count'
number = int(counter.read_text()) + 1 if counter.exists() else 1
counter.write_text(str(number))
cases = os.environ['CASES'].split(',')
with (root / 'runner-calls').open('a') as calls:
    calls.write(os.environ['CASES'] + '\\n')
out = root / f'run-{number}'
out.mkdir()
mode = os.environ['RUNNER_MODE']
actual = list(cases)
if mode == 'partial':
    actual.pop()
elif mode == 'different':
    actual[-1] = '99.99.99'
report = {
    'BlackBull': {
        case_id: {'behavior': 'OK', 'behaviorClose': 'OK'}
        for case_id in actual
    }
}
(out / 'index.json').write_text(json.dumps(report))
print(f'Results: {out}')
if mode == 'nonzero-pass' or (mode == 'retry' and number == 12):
    sys.exit(137)
''')
    env.update({
        'FAKE_ROOT': str(root),
        'RUNNER_MODE': mode,
        'FAKE_PYTHON': sys.executable,
    })
    _script(
        scripts / 'autobahn_run.sh',
        'exec "$FAKE_PYTHON" "$FAKE_ROOT/fake-batch-runner.py"\n',
    )


def test_heavy_rejects_resolver_failure_before_any_batch(harness):
    root, scripts, _, env = harness
    _resolver(harness, exit_code=125)
    _script(scripts / 'autobahn_run.sh', 'touch "$FAKE_ROOT/runner-called"\n')
    env['FAKE_ROOT'] = str(root)

    completed = _run(harness, 'autobahn_heavy.sh')

    assert completed.returncode != 0
    assert not (root / 'runner-called').exists()
    assert (root / 'resolver-cleanup').read_text() == 'resolver-removed\n'


def test_heavy_cleans_resolver_if_create_fails_after_writing_cidfile(harness):
    root, _, _, _ = harness
    _resolver(harness, create_exit=125)

    completed = _run(harness, 'autobahn_heavy.sh')

    assert completed.returncode != 0
    assert (root / 'resolver-cleanup').read_text() == 'resolver-removed\n'


def test_heavy_term_during_create_recovers_resolver_from_cidfile(harness):
    root, _, _, _ = harness
    _resolver(harness, create_exit=143, create_interrupt=True)

    completed = _run(harness, 'autobahn_heavy.sh')

    assert completed.returncode == 143
    assert (root / 'resolver-cleanup').read_text() == 'resolver-removed\n'


def test_heavy_rejects_empty_resolver_cid_before_any_batch(harness):
    root, _, _, _ = harness
    _resolver(harness, cid='')
    _batch_runner(harness, 'pass')

    completed = _run(harness, 'autobahn_heavy.sh')

    assert completed.returncode != 0
    assert not (root / 'runner-calls').exists()


def test_heavy_term_during_resolver_cleans_up_and_fails(harness):
    root, _, _, _ = harness
    _resolver(harness, exit_code=143, interrupt=True)

    completed = _run(harness, 'autobahn_heavy.sh')

    assert completed.returncode == 143
    assert (root / 'resolver-cleanup').read_text() == 'resolver-removed\n'


def test_heavy_fails_if_resolver_container_cannot_be_removed(harness):
    _resolver(harness, remove_exit=7)
    _batch_runner(harness, 'pass')

    completed = _run(harness, 'autobahn_heavy.sh')

    assert completed.returncode != 0


def test_heavy_rejects_nonzero_batch_with_passing_index(harness):
    _resolver(harness)
    _batch_runner(harness, 'nonzero-pass')

    completed = _run(harness, 'autobahn_heavy.sh')

    assert completed.returncode != 0


@pytest.mark.parametrize('mode', ['partial', 'different'])
def test_heavy_rejects_batch_with_incomplete_or_different_ids(harness, mode):
    _resolver(harness)
    _batch_runner(harness, mode)

    completed = _run(harness, 'autobahn_heavy.sh')

    assert completed.returncode != 0


def test_heavy_retry_restarts_every_batch_without_carryover(harness):
    root, _, _, env = harness
    env['MAX_ATTEMPTS'] = '2'
    _resolver(harness)
    _batch_runner(harness, 'retry')

    completed = _run(harness, 'autobahn_heavy.sh')

    assert completed.returncode == 0, completed.stdout + completed.stderr
    calls = (root / 'runner-calls').read_text().splitlines()
    assert len(calls) == 24
    assert calls[:12] == calls[12:]


@pytest.mark.parametrize(('report', 'accepted'), [
    (PASS, True),
    ({'BlackBull': {}}, False),
    ({}, False),
    (None, False),
    ({'BlackBull': {'13.3.10': {'behavior': 'FAILED', 'behaviorClose': 'OK'}}}, False),
])
def test_assert_requires_nonempty_passing_report(harness, report, accepted):
    root, _, _, _ = harness
    result = root / 'bench/conformance/results/autobahn_test'
    result.mkdir(parents=True)
    if report is not None:
        (result / 'index.json').write_text(json.dumps(report))
    completed = _run(harness, 'autobahn_assert.sh')
    assert (completed.returncode == 0) is accepted, completed.stdout + completed.stderr


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


def _docker(
    harness, *, exit_code=0, created=True, interrupt=False,
    inspect_exit=0, remove_exit=0, cid='fake-container',
):
    root, _, bindir, env = harness
    env.update({
        'FAKE_EXIT': str(exit_code),
        'FAKE_CREATED': str(int(created)),
        'FAKE_INTERRUPT': str(int(interrupt)),
        'FAKE_INSPECT_EXIT': str(inspect_exit),
        'FAKE_REMOVE_EXIT': str(remove_exit),
        'FAKE_CID': cid,
        'FAKE_ROOT': str(root),
    })
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
      [[ -z "$cidfile" ]] || printf '%s' "$FAKE_CID" > "$cidfile"
      cp "$FAKE_ROOT/fake-report.json" "$out/index.json"
    fi
    echo 'Running test case ID 13.3.10'
    echo 'tester diagnostic' >&2
    if [[ "$FAKE_INTERRUPT" == 1 ]]; then kill -TERM "$PPID"; fi
    exit "$FAKE_EXIT"
    ;;
  inspect)
    if [[ "$FAKE_INSPECT_EXIT" != 0 ]]; then exit "$FAKE_INSPECT_EXIT"; fi
    printf '{"ExitCode":%s,"OOMKilled":%s,"Running":false}\n' \
      "$FAKE_EXIT" "$([[ "$FAKE_EXIT" == 137 ]] && echo true || echo false)"
    ;;
  rm)
    echo removed >> "$FAKE_ROOT/cleanup"
    exit "$FAKE_REMOVE_EXIT"
    ;;
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


@pytest.mark.parametrize(
    ('runner_exit', 'inspect_exit', 'remove_exit', 'expected_exit'),
    [
        (0, 7, 0, 1),
        (0, 0, 7, 1),
        (137, 7, 7, 137),
    ],
)
def test_run_cleanup_failures_cannot_turn_into_success_or_hide_runner_failure(
    harness, runner_exit, inspect_exit, remove_exit, expected_exit,
):
    root, _, _, _ = harness
    _docker(
        harness,
        exit_code=runner_exit,
        inspect_exit=inspect_exit,
        remove_exit=remove_exit,
    )

    completed = _run(harness, 'autobahn_run.sh')

    assert completed.returncode == expected_exit
    result, = (root / 'bench/conformance/results').iterdir()
    assert (result / 'exit-code.txt').read_text().strip() == str(expected_exit)


@pytest.mark.parametrize(
    ('runner_exit', 'expected_exit'),
    [(0, 1), (137, 137)],
)
def test_run_empty_cid_is_failure_without_hiding_runner_status(
    harness, runner_exit, expected_exit,
):
    root, _, _, _ = harness
    _docker(harness, exit_code=runner_exit, cid='')

    completed = _run(harness, 'autobahn_run.sh')

    assert completed.returncode == expected_exit
    result, = (root / 'bench/conformance/results').iterdir()
    assert (result / 'exit-code.txt').read_text().strip() == str(expected_exit)


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
