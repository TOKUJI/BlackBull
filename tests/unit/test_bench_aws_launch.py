"""Regression tests for detached AWS benchmark runner launches.

The remote shell must return after the runner is started even when the runner
keeps descendants alive.  These tests cover the shell topology locally and
pin the two AWS drivers to the same launch contract.
"""
import os
import subprocess
import time
from pathlib import Path

import pytest


ROOT = Path(__file__).parents[2]


def test_aws_drivers_are_valid_shell_and_use_detached_launch_contract():
    for name in ('ab.sh', 'run.sh'):
        script = ROOT / 'bench' / 'aws' / name
        subprocess.run(('bash', '-n', str(script)), check=True)
        source = script.read_text()
        assert 'ssh -n "${SSH_OPTS[@]}"' in source
        assert 'nohup setsid bash' in source
        assert 'pid=\\$!' in source
        assert 'kill -0' in source
        assert 'timeout --signal=TERM --kill-after=5s' in source
        assert 'ssh -n "${SSH_OPTS[@]}"' in source
        assert 'if ! scp "${SSH_OPTS[@]}"' in source
        assert 'remote runner launch failed' in source
        assert "& echo launched" not in source


def _fake_aws_tools(
    tmp_path: Path, *, fail_scp: bool = False, runner_fails: bool = False
) -> dict[str, str]:
    bindir = tmp_path / 'bin'
    remote = tmp_path / 'remote'
    bindir.mkdir()
    (remote / 'bench' / 'results').mkdir(parents=True)

    for name in ('aws', 'jq'):
        (bindir / name).write_text('#!/usr/bin/env bash\nexit 0\n')
        (bindir / name).chmod(0o755)
    scp_body = 'exit 1\n' if fail_scp else (
        'src="${@: -2:1}"; dest="${@: -1}";\n'
        'if [[ "${FAKE_SCP_DOWNLOAD_FAIL:-0}" == 1 && "$dest" != *:* ]]; then exit 1; fi\n'
        '[[ "$dest" == *:* ]] || exit 0;\n'
        'path="${dest#*:}";\n'
        'path="${path#/home/ubuntu/BlackBull/}";\n'
        'mkdir -p "$FAKE_REMOTE/$(dirname "$path")";\n'
        'cp "$src" "$FAKE_REMOTE/$path"\n'
    )
    (bindir / 'scp').write_text(
        '#!/usr/bin/env bash\nset -eu\n' + scp_body
    )
    (bindir / 'scp').chmod(0o755)
    runner_replacement = (
        r'cmd="${cmd//bash bench\/results\/ab_runner.sh/false}"' + '\n'
        if runner_fails
        else r'cmd="${cmd//bash bench\/results\/ab_runner.sh/sleep 2}"' + '\n'
    )
    (bindir / 'ssh').write_text(
        '#!/usr/bin/env bash\n'
        'set -eu\n'
        'cmd="${@: -1}"\n'
        'printf "%s" "$cmd" > "$FAKE_SSH_COMMAND"\n'
        'if [[ "$cmd" == *"command -v uv"* ]]; then exit 0; fi\n'
        'if [[ "${FAKE_SSH_METADATA_FAIL:-0}" == 1 && "$cmd" == *"ab_expected_lines"* ]]; then exit 255; fi\n'
        'if [[ "$cmd" == *"pgrep -f"* ]]; then\n'
        '  if [[ -n "${FAKE_SSH_POLL_COUNT:-}" ]]; then\n'
        '    count=0\n'
        '    [[ ! -f "$FAKE_SSH_POLL_COUNT" ]] || read -r count < "$FAKE_SSH_POLL_COUNT"\n'
        '    printf "%s\\n" "$((count + 1))" > "$FAKE_SSH_POLL_COUNT"\n'
        '  fi\n'
        '  if [[ "${FAKE_SSH_POLL_FAIL:-0}" == 1 ]]; then exit 255; fi\n'
        '  if [[ -n "${FAKE_SSH_POLL_STATE:-}" ]]; then printf "%s\\n" "$FAKE_SSH_POLL_STATE"; exit 0; fi\n'
        'fi\n'
        'if [[ "${FAKE_SSH_RESULT_LIST_FAIL:-0}" == 1 && "$cmd" == *"find bench/results"* ]]; then exit 255; fi\n'
        'if [[ "${FAKE_SSH_HANG:-0}" == 1 ]]; then exec sleep 5; fi\n'
        r'cmd="${cmd//\/home\/ubuntu\/BlackBull/$FAKE_REMOTE}"' + '\n'
        + runner_replacement
        + 'bash -c "$cmd"\n'
    )
    (bindir / 'ssh').chmod(0o755)
    return {
        'PATH': f'{bindir}:{os.environ["PATH"]}',
        'FAKE_REMOTE': str(remote),
        'FAKE_SSH_COMMAND': str(tmp_path / 'ssh-command'),
    }


def _wait_for_runner_status(results: Path) -> None:
    for _ in range(40):
        if (results / 'ab_runner.status').exists():
            break
        time.sleep(0.1)
    assert (results / 'ab_runner.status').read_text() == '0\n'


def test_ab_launch_detaches_remote_runner_and_returns(tmp_path):
    env = _fake_aws_tools(tmp_path)
    state = tmp_path / 'state'
    state.write_text('SERVER_PUBLIC_IP=fake\n')
    env.update({
        'STATE_FILE': str(state),
        'AB_LAUNCH_TIMEOUT': '2',
        'REF_BASE': 'HEAD~1',
        'REF_TREAT': 'HEAD',
        'PATHSPEC': 'blackbull/',
    })

    completed = subprocess.run(
        ('bash', str(ROOT / 'bench/aws/ab.sh'), 'launch'),
        check=True,
        capture_output=True,
        text=True,
        timeout=5,
        env=env,
    )
    assert 'ab_commit.sh launched' in completed.stdout
    command = (tmp_path / 'ssh-command').read_text()
    assert 'nohup setsid bash -c' in command
    assert 'bench/results/ab_runner.pid' in command
    assert 'ab_expected_lines' in command
    assert 'ab_expected_results' in command


def test_ab_launch_records_expected_lines_for_selected_phases(tmp_path):
    env = _fake_aws_tools(tmp_path)
    state = tmp_path / 'state'
    state.write_text('SERVER_PUBLIC_IP=fake\n')
    env.update({
        'STATE_FILE': str(state),
        'ROUNDS': '4',
        'PHASES': 'real',
        'AB_LAUNCH_TIMEOUT': '2',
    })

    subprocess.run(
        ('bash', str(ROOT / 'bench/aws/ab.sh'), 'launch'),
        check=True,
        capture_output=True,
        text=True,
        timeout=5,
        env=env,
    )

    assert (
        Path(env['FAKE_REMOTE']) / 'bench' / 'results' / 'ab_expected_lines'
    ).read_text() == '17\n'
    assert (
        Path(env['FAKE_REMOTE']) / 'bench' / 'results' / 'ab_expected_results'
    ).read_text() == '1\n'


def test_ab_launch_counts_phases_across_shell_whitespace(tmp_path):
    env = _fake_aws_tools(tmp_path)
    state = tmp_path / 'state'
    state.write_text('SERVER_PUBLIC_IP=fake\n')
    env.update({
        'STATE_FILE': str(state),
        'ROUNDS': '1',
        'PHASES': 'null\nreal',
        'AB_LAUNCH_TIMEOUT': '2',
    })

    subprocess.run(
        ('bash', str(ROOT / 'bench/aws/ab.sh'), 'launch'),
        check=True,
        capture_output=True,
        text=True,
        timeout=5,
        env=env,
    )

    assert (
        Path(env['FAKE_REMOTE']) / 'bench' / 'results' / 'ab_expected_lines'
    ).read_text() == '9\n'


@pytest.mark.parametrize(
    ('override', 'message'),
    (
        pytest.param(
            {'ROUNDS': '0'},
            'ROUNDS must be a positive integer',
            id='zero-rounds',
        ),
        pytest.param(
            {'ROUNDS': '1+1'},
            'ROUNDS must be a positive integer',
            id='arithmetic-expression-rounds',
        ),
        pytest.param(
            {'PHASES': '   '},
            'PHASES must select at least one phase',
            id='no-phases',
        ),
        pytest.param(
            {'PHASES': 'null reel'},
            'PHASES must contain only null and real',
            id='unknown-phase',
        ),
    ),
)
def test_ab_launch_rejects_invalid_measurement_configuration(
    tmp_path, override, message
):
    env = _fake_aws_tools(tmp_path)
    state = tmp_path / 'state'
    state.write_text('SERVER_PUBLIC_IP=fake\n')
    env.update({'STATE_FILE': str(state), 'AB_LAUNCH_TIMEOUT': '2', **override})

    completed = subprocess.run(
        ('bash', str(ROOT / 'bench/aws/ab.sh'), 'launch'),
        capture_output=True,
        text=True,
        timeout=5,
        env=env,
    )

    assert completed.returncode == 1
    assert message in completed.stderr


@pytest.mark.parametrize(
    ('override', 'message'),
    (
        pytest.param(
            {'URL_PATHS': '/one,,/two'},
            'URL_PATHS must contain only non-empty paths',
            id='empty-h1-profile',
        ),
        pytest.param(
            {'URL_PATHS': '/one,'},
            'URL_PATHS must contain only non-empty paths',
            id='trailing-empty-h1-profile',
        ),
        pytest.param(
            {'H2_PROFILES': ',/h2'},
            'H2_PROFILES must contain only non-empty paths',
            id='empty-h2-profile',
        ),
        pytest.param(
            {'H2_PROFILES': '/h2,'},
            'H2_PROFILES must contain only non-empty paths',
            id='trailing-empty-h2-profile',
        ),
    ),
)
def test_ab_launch_rejects_empty_profile_entries(tmp_path, override, message):
    env = _fake_aws_tools(tmp_path)
    state = tmp_path / 'state'
    state.write_text('SERVER_PUBLIC_IP=fake\n')
    env.update({'STATE_FILE': str(state), **override})

    completed = subprocess.run(
        ('bash', str(ROOT / 'bench/aws/ab.sh'), 'launch'),
        capture_output=True,
        text=True,
        timeout=5,
        env=env,
    )

    assert completed.returncode == 1
    assert message in completed.stderr


def test_ab_launch_reports_upload_failure(tmp_path):
    env = _fake_aws_tools(tmp_path, fail_scp=True)
    state = tmp_path / 'state'
    state.write_text('SERVER_PUBLIC_IP=fake\n')
    env.update({'STATE_FILE': str(state)})

    completed = subprocess.run(
        ('bash', str(ROOT / 'bench/aws/ab.sh'), 'launch'),
        capture_output=True,
        text=True,
        env=env,
    )
    assert completed.returncode == 1
    assert 'runner upload failed' in completed.stderr


def test_ab_launch_reports_runner_exit_during_handshake(tmp_path):
    env = _fake_aws_tools(tmp_path, runner_fails=True)
    state = tmp_path / 'state'
    state.write_text('SERVER_PUBLIC_IP=fake\n')
    env.update({'STATE_FILE': str(state), 'AB_LAUNCH_TIMEOUT': '2'})

    completed = subprocess.run(
        ('bash', str(ROOT / 'bench/aws/ab.sh'), 'launch'),
        capture_output=True,
        text=True,
        env=env,
    )
    assert completed.returncode == 1
    assert 'remote runner launch failed' in completed.stderr


def test_ab_launch_has_a_bounded_ssh_handshake(tmp_path):
    env = _fake_aws_tools(tmp_path)
    state = tmp_path / 'state'
    state.write_text('SERVER_PUBLIC_IP=fake\n')
    env.update({
        'STATE_FILE': str(state),
        'AB_LAUNCH_TIMEOUT': '1',
        'FAKE_SSH_HANG': '1',
    })

    completed = subprocess.run(
        ('bash', str(ROOT / 'bench/aws/ab.sh'), 'launch'),
        capture_output=True,
        text=True,
        timeout=8,
        env=env,
    )
    assert completed.returncode == 1
    assert 'remote runner launch failed' in completed.stderr


def test_ab_finish_uses_launch_expected_lines_when_rounds_differs(tmp_path):
    env = _fake_aws_tools(tmp_path)
    state = tmp_path / 'state'
    state.write_text('SERVER_PUBLIC_IP=fake\n')
    env.update({
        'STATE_FILE': str(state),
        'ROUNDS': '4',
        'AB_LAUNCH_TIMEOUT': '2',
    })

    subprocess.run(
        ('bash', str(ROOT / 'bench/aws/ab.sh'), 'launch'),
        check=True,
        capture_output=True,
        text=True,
        timeout=5,
        env=env,
    )

    remote = Path(env['FAKE_REMOTE'])
    results = remote / 'bench' / 'results'
    assert (results / 'ab_expected_lines').read_text() == '33\n'
    _wait_for_runner_status(results)
    (results / 'ab-commit-smoke').mkdir()
    (results / 'ab-commit-smoke' / 'raw.tsv').write_text('header\n' + 'row\n' * 32)
    finish_log = tmp_path / 'finish.log'
    env.update({
        'ROUNDS': '8',
        'AB_POLLS': '1',
        'AB_POLL_INTERVAL': '0',
        'AB_FINISH_LOG': str(finish_log),
        'TEARDOWN': '0',
    })

    completed = subprocess.run(
        ('bash', str(ROOT / 'bench/aws/ab.sh'), 'finish'),
        check=True,
        capture_output=True,
        text=True,
        timeout=5,
        env=env,
    )

    assert 'finish running' in completed.stdout
    log = finish_log.read_text()
    assert 'expected raw.tsv lines: 33' in log
    assert 'complete_rawtsv=1' in log


@pytest.mark.parametrize(
    ('present_results', 'complete_results', 'should_succeed'),
    (
        pytest.param(1, 1, False, id='two-of-three-missing'),
        pytest.param(3, 2, False, id='one-of-three-incomplete'),
        pytest.param(3, 3, True, id='all-three-complete'),
    ),
)
def test_ab_finish_multi_lane_complete_state(
    tmp_path, present_results, complete_results, should_succeed
):
    env = _fake_aws_tools(tmp_path)
    state = tmp_path / 'state'
    state.write_text('SERVER_PUBLIC_IP=fake\n')
    env.update({
        'STATE_FILE': str(state),
        'ROUNDS': '1',
        'URL_PATHS': '/one,/two',
        'H2_PROFILES': '/h2',
        'AB_LAUNCH_TIMEOUT': '2',
    })

    subprocess.run(
        ('bash', str(ROOT / 'bench/aws/ab.sh'), 'launch'),
        check=True,
        capture_output=True,
        text=True,
        timeout=5,
        env=env,
    )

    remote = Path(env['FAKE_REMOTE'])
    results = remote / 'bench' / 'results'
    _wait_for_runner_status(results)
    names = ('ab-commit-one', 'ab-commit-two', 'ab-h2-three')
    for index, name in enumerate(names[:present_results]):
        result = results / name
        result.mkdir()
        rows = 8 if index < complete_results else 7
        (result / 'raw.tsv').write_text('header\n' + 'row\n' * rows)

    finish_log = tmp_path / 'finish.log'
    env.update({
        # Finish must use the launch-time lane count, not reconstruct it from
        # a later shell whose profile variables may differ or be absent.
        'URL_PATHS': '/finish-only',
        'H2_PROFILES': '',
        'AB_POLLS': '1',
        'AB_POLL_INTERVAL': '0',
        'AB_FINISH_LOG': str(finish_log),
        'TEARDOWN': '0',
    })
    completed = subprocess.run(
        ('bash', str(ROOT / 'bench/aws/ab.sh'), 'finish'),
        capture_output=True,
        text=True,
        timeout=5,
        env=env,
    )

    assert (completed.returncode == 0) is should_succeed
    if not should_succeed:
        assert 'not all configured A/B results are complete' in finish_log.read_text()


def test_ab_finish_current_run_rejects_an_extra_result_directory(tmp_path):
    env = _fake_aws_tools(tmp_path)
    state = tmp_path / 'state'
    state.write_text('SERVER_PUBLIC_IP=fake\n')
    remote = Path(env['FAKE_REMOTE'])
    results = remote / 'bench' / 'results'
    for name in ('ab-commit-expected', 'ab-commit-extra'):
        result = results / name
        result.mkdir()
        (result / 'raw.tsv').write_text('header\n' + 'row\n' * 8)
    (results / 'ab_expected_lines').write_text('9\n')
    (results / 'ab_expected_results').write_text('1\n')
    (results / 'ab_runner.status.required').write_text('')
    (results / 'ab_runner.status').write_text('0\n')
    finish_log = tmp_path / 'finish.log'
    env.update({
        'STATE_FILE': str(state),
        'ROUNDS': '1',
        'AB_POLLS': '1',
        'AB_POLL_INTERVAL': '0',
        'AB_FINISH_LOG': str(finish_log),
        'TEARDOWN': '0',
    })

    completed = subprocess.run(
        ('bash', str(ROOT / 'bench/aws/ab.sh'), 'finish'),
        capture_output=True,
        text=True,
        timeout=5,
        env=env,
    )

    assert completed.returncode == 1
    assert 'not all configured A/B results are complete' in finish_log.read_text()


def test_ab_finish_falls_back_when_launch_metadata_is_missing(tmp_path):
    env = _fake_aws_tools(tmp_path)
    state = tmp_path / 'state'
    state.write_text('SERVER_PUBLIC_IP=fake\n')
    remote = Path(env['FAKE_REMOTE'])
    results = remote / 'bench' / 'results'
    (results / 'ab-commit-legacy').mkdir()
    (results / 'ab-commit-legacy' / 'raw.tsv').write_text('header\n' + 'row\n' * 8)
    finish_log = tmp_path / 'finish.log'
    env.update({
        'STATE_FILE': str(state),
        'ROUNDS': '1',
        'AB_POLLS': '1',
        'AB_POLL_INTERVAL': '0',
        'AB_FINISH_LOG': str(finish_log),
        'TEARDOWN': '0',
    })

    completed = subprocess.run(
        ('bash', str(ROOT / 'bench/aws/ab.sh'), 'finish'),
        check=True,
        capture_output=True,
        text=True,
        timeout=5,
        env=env,
    )

    assert 'finish running' in completed.stdout
    log = finish_log.read_text()
    assert 'expected raw.tsv lines: 9' in log
    assert 'complete_rawtsv=1' in log


def test_ab_finish_legacy_run_requires_every_discovered_result(tmp_path):
    env = _fake_aws_tools(tmp_path)
    state = tmp_path / 'state'
    state.write_text('SERVER_PUBLIC_IP=fake\n')
    remote = Path(env['FAKE_REMOTE'])
    results = remote / 'bench' / 'results'
    for name, rows in (('ab-commit-complete', 8), ('ab-h2-incomplete', 7)):
        result = results / name
        result.mkdir()
        (result / 'raw.tsv').write_text('header\n' + 'row\n' * rows)
    finish_log = tmp_path / 'finish.log'
    env.update({
        'STATE_FILE': str(state),
        'ROUNDS': '1',
        'AB_POLLS': '1',
        'AB_POLL_INTERVAL': '0',
        'AB_FINISH_LOG': str(finish_log),
        'TEARDOWN': '0',
    })

    completed = subprocess.run(
        ('bash', str(ROOT / 'bench/aws/ab.sh'), 'finish'),
        capture_output=True,
        text=True,
        timeout=5,
        env=env,
    )

    assert completed.returncode == 1
    assert 'not all configured A/B results are complete' in finish_log.read_text()


def test_ab_finish_explicit_expected_lines_supports_legacy_recovery(tmp_path):
    env = _fake_aws_tools(tmp_path)
    state = tmp_path / 'state'
    state.write_text('SERVER_PUBLIC_IP=fake\n')
    remote = Path(env['FAKE_REMOTE'])
    result = remote / 'bench' / 'results' / 'ab-commit-legacy'
    result.mkdir()
    (result / 'raw.tsv').write_text('header\nrow\nrow\nrow\n')
    finish_log = tmp_path / 'finish.log'
    env.update({
        'STATE_FILE': str(state),
        'EXPECT_LINES': '4',
        'AB_POLLS': '1',
        'AB_POLL_INTERVAL': '0',
        'AB_FINISH_LOG': str(finish_log),
        'TEARDOWN': '0',
    })

    completed = subprocess.run(
        ('bash', str(ROOT / 'bench/aws/ab.sh'), 'finish'),
        check=True,
        capture_output=True,
        text=True,
        timeout=5,
        env=env,
    )

    assert 'expected raw.tsv lines: 4' in finish_log.read_text()


def test_ab_finish_explicit_expected_lines_overrides_launch_metadata(tmp_path):
    env = _fake_aws_tools(tmp_path)
    state = tmp_path / 'state'
    state.write_text('SERVER_PUBLIC_IP=fake\n')
    remote = Path(env['FAKE_REMOTE'])
    results = remote / 'bench' / 'results'
    (results / 'ab-commit-smoke').mkdir()
    (results / 'ab-commit-smoke' / 'raw.tsv').write_text('header\nrow\nrow\nrow\nrow\n')
    (results / 'ab_expected_lines').write_text('65\n')
    (results / 'ab_expected_results').write_text('1\n')
    (results / 'ab_runner.status.required').write_text('')
    (results / 'ab_runner.status').write_text('0\n')
    finish_log = tmp_path / 'finish.log'
    env.update({
        'STATE_FILE': str(state),
        'ROUNDS': '8',
        'EXPECT_LINES': '4',
        'AB_POLLS': '1',
        'AB_POLL_INTERVAL': '0',
        'AB_FINISH_LOG': str(finish_log),
        'TEARDOWN': '0',
    })

    completed = subprocess.run(
        ('bash', str(ROOT / 'bench/aws/ab.sh'), 'finish'),
        check=True,
        capture_output=True,
        text=True,
        timeout=5,
        env=env,
    )

    assert 'finish running' in completed.stdout
    log = finish_log.read_text()
    assert 'expected raw.tsv lines: 4' in log
    assert 'complete_rawtsv=1' in log


@pytest.mark.parametrize('runner_status', ('missing', '0'))
def test_ab_finish_explicit_expected_lines_keeps_protocol_checks(
    tmp_path, runner_status
):
    env = _fake_aws_tools(tmp_path)
    state = tmp_path / 'state'
    state.write_text('SERVER_PUBLIC_IP=fake\n')
    remote = Path(env['FAKE_REMOTE'])
    results = remote / 'bench' / 'results'
    (results / 'ab-commit-smoke').mkdir()
    (results / 'ab-commit-smoke' / 'raw.tsv').write_text(
        'header\nrow\nrow\nrow\nrow\n'
    )
    (results / 'ab_expected_lines').write_text('65\n')
    (results / 'ab_expected_results').write_text('1\n')
    if runner_status != 'missing':
        (results / 'ab_runner.status').write_text(f'{runner_status}\n')
    finish_log = tmp_path / 'finish.log'
    poll_count = tmp_path / 'poll-count'
    env.update({
        'STATE_FILE': str(state),
        'EXPECT_LINES': '4',
        'FAKE_SSH_POLL_COUNT': str(poll_count),
        'AB_POLLS': '3',
        'AB_POLL_INTERVAL': '0',
        'AB_FINISH_LOG': str(finish_log),
        'TEARDOWN': '0',
    })

    completed = subprocess.run(
        ('bash', str(ROOT / 'bench/aws/ab.sh'), 'finish'),
        capture_output=True,
        text=True,
        timeout=5,
        env=env,
    )

    assert completed.returncode == 1
    assert int(poll_count.read_text()) == 1
    assert 'protocol marker is missing' in finish_log.read_text()


# A current-format run is complete iff all four terminal facts agree:
# launch metadata exists, the status protocol is required, the runner has
# stopped successfully, and every launch-declared raw result reached its
# line count.  Each invalid current-format row below changes exactly one of
# those facts.  Once the runner has stopped, the state is terminal and must
# be accepted or rejected after one poll; only a running process may consume
# more of the poll budget.  The legacy row is the sole compatibility exception.
@pytest.mark.parametrize(
    (
        'metadata',
        'runner',
        'total',
        'complete',
        'status_required',
        'runner_status',
        'should_succeed',
        'expected_polls',
    ),
    (
        pytest.param('valid', 0, 1, 1, 1, '0', True, 1, id='current-complete'),
        pytest.param('missing', 0, 1, 1, 0, 'missing', True, 1, id='legacy-complete'),
        pytest.param('valid', 1, 0, 0, 1, 'missing', False, 3, id='current-running'),
        pytest.param('valid', 0, 1, 0, 1, '0', False, 1, id='current-result-incomplete'),
        pytest.param('valid', 0, 1, 1, 0, '0', False, 1, id='current-protocol-marker-missing'),
        pytest.param('valid', 0, 1, 1, 1, 'missing', False, 1, id='current-status-missing'),
        pytest.param('valid', 0, 1, 1, 1, '1', False, 1, id='current-runner-failed'),
        pytest.param('valid', 0, 1, 1, 1, 'garbage', False, 1, id='current-status-malformed'),
        pytest.param('missing', 0, 1, 1, 1, '0', False, 0, id='current-metadata-missing'),
    ),
)
def test_ab_finish_complete_state_contract(
    tmp_path,
    metadata,
    runner,
    total,
    complete,
    status_required,
    runner_status,
    should_succeed,
    expected_polls,
):
    env = _fake_aws_tools(tmp_path)
    state = tmp_path / 'state'
    state.write_text('SERVER_PUBLIC_IP=fake\n')
    remote = Path(env['FAKE_REMOTE'])
    results = remote / 'bench' / 'results'
    result = results / 'ab-commit-state-contract'
    result.mkdir()
    (result / 'raw.tsv').write_text('header\n' + 'row\n' * 64)

    if metadata == 'valid':
        (results / 'ab_expected_lines').write_text('65\n')
        (results / 'ab_expected_results').write_text('1\n')
    if status_required:
        (results / 'ab_runner.status.required').write_text('')
    if runner_status != 'missing':
        (results / 'ab_runner.status').write_text(f'{runner_status}\n')

    poll_count = tmp_path / 'poll-count'
    finish_log = tmp_path / 'finish.log'
    env.update({
        'STATE_FILE': str(state),
        'FAKE_SSH_POLL_STATE': (
            f'{runner} {total} {complete} {status_required} {runner_status}'
        ),
        'FAKE_SSH_POLL_COUNT': str(poll_count),
        'AB_POLLS': '3',
        'AB_POLL_INTERVAL': '0',
        'AB_FINISH_LOG': str(finish_log),
        'TEARDOWN': '0',
    })

    completed_process = subprocess.run(
        ('bash', str(ROOT / 'bench/aws/ab.sh'), 'finish'),
        capture_output=True,
        text=True,
        timeout=5,
        env=env,
    )

    assert (completed_process.returncode == 0) is should_succeed
    actual_polls = int(poll_count.read_text()) if poll_count.exists() else 0
    assert actual_polls == expected_polls


def test_ab_finish_fails_when_remote_poll_fails(tmp_path):
    env = _fake_aws_tools(tmp_path)
    state = tmp_path / 'state'
    state.write_text('SERVER_PUBLIC_IP=fake\n')
    finish_log = tmp_path / 'finish.log'
    env.update({
        'STATE_FILE': str(state),
        'FAKE_SSH_POLL_FAIL': '1',
        'AB_FINISH_LOG': str(finish_log),
    })

    completed = subprocess.run(
        ('bash', str(ROOT / 'bench/aws/ab.sh'), 'finish'),
        capture_output=True,
        text=True,
        timeout=5,
        env=env,
    )

    assert completed.returncode == 1
    assert 'finish failed; details ->' in completed.stderr
    assert 'failed to poll remote runner state' in finish_log.read_text()


def test_ab_finish_fails_when_remote_result_list_fails(tmp_path):
    env = _fake_aws_tools(tmp_path)
    state = tmp_path / 'state'
    state.write_text('SERVER_PUBLIC_IP=fake\n')
    remote = Path(env['FAKE_REMOTE'])
    results = remote / 'bench' / 'results'
    (results / 'ab-commit-smoke').mkdir()
    (results / 'ab-commit-smoke' / 'raw.tsv').write_text('header\n' + 'row\n' * 64)
    finish_log = tmp_path / 'finish.log'
    env.update({
        'STATE_FILE': str(state),
        'FAKE_SSH_RESULT_LIST_FAIL': '1',
        'AB_POLLS': '1',
        'AB_POLL_INTERVAL': '0',
        'AB_FINISH_LOG': str(finish_log),
        'TEARDOWN': '0',
    })

    completed = subprocess.run(
        ('bash', str(ROOT / 'bench/aws/ab.sh'), 'finish'),
        capture_output=True,
        text=True,
        timeout=5,
        env=env,
    )

    assert completed.returncode == 1
    assert 'finish failed; details ->' in completed.stderr
    assert 'failed to list remote A/B results' in finish_log.read_text()


def test_ab_finish_fails_when_result_copy_fails(tmp_path):
    env = _fake_aws_tools(tmp_path)
    state = tmp_path / 'state'
    state.write_text('SERVER_PUBLIC_IP=fake\n')
    remote = Path(env['FAKE_REMOTE'])
    results = remote / 'bench' / 'results'
    (results / 'ab-commit-smoke').mkdir()
    (results / 'ab-commit-smoke' / 'raw.tsv').write_text('header\n' + 'row\n' * 64)
    finish_log = tmp_path / 'finish.log'
    env.update({
        'STATE_FILE': str(state),
        'FAKE_SCP_DOWNLOAD_FAIL': '1',
        'AB_POLLS': '1',
        'AB_POLL_INTERVAL': '0',
        'AB_FINISH_LOG': str(finish_log),
        'TEARDOWN': '0',
    })

    completed = subprocess.run(
        ('bash', str(ROOT / 'bench/aws/ab.sh'), 'finish'),
        capture_output=True,
        text=True,
        timeout=5,
        env=env,
    )

    assert completed.returncode == 1
    assert 'finish failed; details ->' in completed.stderr
    assert 'failed to copy A/B result' in finish_log.read_text()


def test_ab_finish_fails_when_runner_status_is_nonzero(tmp_path):
    env = _fake_aws_tools(tmp_path)
    state = tmp_path / 'state'
    state.write_text('SERVER_PUBLIC_IP=fake\n')
    remote = Path(env['FAKE_REMOTE'])
    results = remote / 'bench' / 'results'
    (results / 'ab-commit-smoke').mkdir()
    (results / 'ab-commit-smoke' / 'raw.tsv').write_text('header\n' + 'row\n' * 64)
    (results / 'ab_expected_lines').write_text('65\n')
    (results / 'ab_expected_results').write_text('1\n')
    (results / 'ab_runner.status.required').write_text('')
    (results / 'ab_runner.status').write_text('1\n')
    finish_log = tmp_path / 'finish.log'
    env.update({
        'STATE_FILE': str(state),
        'AB_POLLS': '1',
        'AB_POLL_INTERVAL': '0',
        'AB_FINISH_LOG': str(finish_log),
        'TEARDOWN': '0',
    })

    completed = subprocess.run(
        ('bash', str(ROOT / 'bench/aws/ab.sh'), 'finish'),
        capture_output=True,
        text=True,
        timeout=5,
        env=env,
    )

    assert completed.returncode == 1
    assert 'finish failed; details ->' in completed.stderr
    assert 'remote A/B runner failed (status=1)' in finish_log.read_text()


def test_ab_finish_fails_when_new_runner_success_marker_is_missing(tmp_path):
    env = _fake_aws_tools(tmp_path)
    state = tmp_path / 'state'
    state.write_text('SERVER_PUBLIC_IP=fake\n')
    remote = Path(env['FAKE_REMOTE'])
    results = remote / 'bench' / 'results'
    (results / 'ab-commit-smoke').mkdir()
    (results / 'ab-commit-smoke' / 'raw.tsv').write_text('header\n' + 'row\n' * 64)
    (results / 'ab_expected_lines').write_text('65\n')
    (results / 'ab_expected_results').write_text('1\n')
    (results / 'ab_runner.status.required').write_text('')
    finish_log = tmp_path / 'finish.log'
    env.update({
        'STATE_FILE': str(state),
        'AB_POLLS': '1',
        'AB_POLL_INTERVAL': '0',
        'AB_FINISH_LOG': str(finish_log),
        'TEARDOWN': '0',
    })

    completed = subprocess.run(
        ('bash', str(ROOT / 'bench/aws/ab.sh'), 'finish'),
        capture_output=True,
        text=True,
        timeout=5,
        env=env,
    )

    assert completed.returncode == 1
    assert 'finish failed; details ->' in completed.stderr
    assert 'success marker is missing' in finish_log.read_text()


def test_ab_finish_fails_when_launch_metadata_cannot_be_read(tmp_path):
    env = _fake_aws_tools(tmp_path)
    state = tmp_path / 'state'
    state.write_text('SERVER_PUBLIC_IP=fake\n')
    finish_log = tmp_path / 'finish.log'
    env.update({
        'STATE_FILE': str(state),
        'FAKE_SSH_METADATA_FAIL': '1',
        'AB_FINISH_LOG': str(finish_log),
    })

    completed = subprocess.run(
        ('bash', str(ROOT / 'bench/aws/ab.sh'), 'finish'),
        capture_output=True,
        text=True,
        timeout=5,
        env=env,
    )

    assert completed.returncode == 1
    assert 'finish failed; details ->' in completed.stderr
    assert 'failed to read launch metadata' in finish_log.read_text()


def test_ab_finish_fails_on_malformed_launch_metadata(tmp_path):
    env = _fake_aws_tools(tmp_path)
    state = tmp_path / 'state'
    state.write_text('SERVER_PUBLIC_IP=fake\n')
    remote = Path(env['FAKE_REMOTE'])
    results = remote / 'bench' / 'results'
    (results / 'ab_expected_lines').write_text('missing\n')
    (results / 'ab_expected_results').write_text('1\n')
    (results / 'ab_runner.status.required').write_text('')
    finish_log = tmp_path / 'finish.log'
    env.update({
        'STATE_FILE': str(state),
        'AB_FINISH_LOG': str(finish_log),
    })

    completed = subprocess.run(
        ('bash', str(ROOT / 'bench/aws/ab.sh'), 'finish'),
        capture_output=True,
        text=True,
        timeout=5,
        env=env,
    )

    assert completed.returncode == 1
    assert 'invalid launch metadata' in finish_log.read_text()


def test_ab_finish_fails_on_zero_launch_metadata(tmp_path):
    env = _fake_aws_tools(tmp_path)
    state = tmp_path / 'state'
    state.write_text('SERVER_PUBLIC_IP=fake\n')
    remote = Path(env['FAKE_REMOTE'])
    results = remote / 'bench' / 'results'
    (results / 'ab_expected_lines').write_text('0\n')
    (results / 'ab_expected_results').write_text('1\n')
    (results / 'ab_runner.status.required').write_text('')
    finish_log = tmp_path / 'finish.log'
    env.update({
        'STATE_FILE': str(state),
        'AB_FINISH_LOG': str(finish_log),
    })

    completed = subprocess.run(
        ('bash', str(ROOT / 'bench/aws/ab.sh'), 'finish'),
        capture_output=True,
        text=True,
        timeout=5,
        env=env,
    )

    assert completed.returncode == 1
    assert 'invalid launch metadata' in finish_log.read_text()


@pytest.mark.parametrize(
    ('value', 'message'),
    (
        pytest.param(None, 'launch metadata is missing', id='missing'),
        pytest.param('0\n', 'invalid launch metadata', id='zero'),
        pytest.param('many\n', 'invalid launch metadata', id='malformed'),
    ),
)
def test_ab_finish_rejects_invalid_expected_result_count_metadata(
    tmp_path, value, message
):
    env = _fake_aws_tools(tmp_path)
    state = tmp_path / 'state'
    state.write_text('SERVER_PUBLIC_IP=fake\n')
    remote = Path(env['FAKE_REMOTE'])
    results = remote / 'bench' / 'results'
    (results / 'ab_expected_lines').write_text('65\n')
    if value is not None:
        (results / 'ab_expected_results').write_text(value)
    (results / 'ab_runner.status.required').write_text('')
    finish_log = tmp_path / 'finish.log'
    env.update({
        'STATE_FILE': str(state),
        'AB_FINISH_LOG': str(finish_log),
    })

    completed = subprocess.run(
        ('bash', str(ROOT / 'bench/aws/ab.sh'), 'finish'),
        capture_output=True,
        text=True,
        timeout=5,
        env=env,
    )

    assert completed.returncode == 1
    assert message in finish_log.read_text()
