"""Regression tests for detached AWS benchmark runner launches.

The remote shell must return after the runner is started even when the runner
keeps descendants alive.  These tests cover the shell topology locally and
pin the two AWS drivers to the same launch contract.
"""
import os
import subprocess
from pathlib import Path


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
        'src="${@: -2:1}"; dest="${@: -1}"; path="${dest#*:}";\n'
        'path="${path#/home/ubuntu/BlackBull/}";\n'
        'mkdir -p "$FAKE_REMOTE/$(dirname "$path")";\n'
        'cp "$src" "$FAKE_REMOTE/$path"\n'
    )
    (bindir / 'scp').write_text(
        '#!/usr/bin/env bash\nset -eu\n' + scp_body
    )
    (bindir / 'scp').chmod(0o755)
    runner_replacement = (
        r'cmd="${cmd//exec bash bench\/results\/ab_runner.sh/exec false}"' + '\n'
        if runner_fails
        else r'cmd="${cmd//exec bash bench\/results\/ab_runner.sh/exec sleep 2}"' + '\n'
    )
    (bindir / 'ssh').write_text(
        '#!/usr/bin/env bash\n'
        'set -eu\n'
        'cmd="${@: -1}"\n'
        'printf "%s" "$cmd" > "$FAKE_SSH_COMMAND"\n'
        'if [[ "$cmd" == *"command -v uv"* ]]; then exit 0; fi\n'
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
