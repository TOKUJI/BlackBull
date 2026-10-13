"""bench/cloud/session.sh: one cloud session, provision to verified teardown.

The instance is a local directory: fake ssh runs the command locally, fake scp
and rsync copy files, and up/install/down are stubs that record their calls.
The session and its helpers are the real scripts.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]

CONFIG = r'''
CLOUD_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$CLOUD_DIR/../.." && pwd)"
STATE_DIR="$REPO_ROOT/bench/.state"
: "${STATE_FILE:=$STATE_DIR/aws}"
export STATE_FILE
SSH_USER=tester
SSH_OPTS=()
: "${REMOTE_REPO:?}"
INSTANCE_TYPE=test.large
_bench_aws_check_env() { return 0; }
_bench_aws_load_state() { source "$STATE_FILE"; }
'''

UP = '''#!/usr/bin/env bash
echo up >> "$CALLS"; mkdir -p "$(dirname "$STATE_FILE")"
printf 'SERVER_PUBLIC_IP=fake\\nTOPO=single\\n' > "$STATE_FILE"
'''
INSTALL = '#!/usr/bin/env bash\necho "install DEPLOY_GIT=${DEPLOY_GIT:-0}" >> "$CALLS"\n'
DOWN = '''#!/usr/bin/env bash
echo down >> "$CALLS"
[ "${FAKE_DOWN_FAILS:-0}" = 1 ] && exit 1
printf 'verified\\n' > "$STATE_FILE.clean"; rm -f "$STATE_FILE"
'''

JOB = r'''
job_preflight() {
    JOB_SETTINGS=(sleep="$FAKE_SLEEP" rc="$FAKE_RC")
    JOB_WAIT_SECONDS="${FAKE_WAIT:-20}"
    JOB_DEPLOY_GIT=0
}
job_runner() {
    printf 'echo partial > %q\n' "$REMOTE_RUN/partial.txt"
    printf 'sleep %q\n' "$FAKE_SLEEP"
    printf 'echo done > %q\n' "$REMOTE_RUN/result.txt"
    printf 'exit %q\n' "$FAKE_RC"
}
job_collect() { [ -f "$LOCAL_RUN/result.txt" ]; }
'''

SSH = r'''#!/usr/bin/env bash
cmd="${@: -1}"
echo "$cmd" >> "$FAKE_SSH_LOG"
# The instance's fail-safe shutdown must never reach the test host.
[[ "$cmd" == *shutdown* ]] && exit 0
if [[ "$cmd" == *": remote-state"* && -f "$FAKE_SSH_FAILURES" ]]; then
    n=$(cat "$FAKE_SSH_FAILURES")
    if [ "$n" -gt 0 ]; then echo $((n - 1)) > "$FAKE_SSH_FAILURES"; exit 255; fi
fi
exec bash -c "$cmd"
'''
SCP = r'''#!/usr/bin/env bash
src="${@: -2:1}"; dest="${@: -1}"
cp "${src#*@fake:}" "${dest#*@fake:}"
'''
RSYNC = r'''#!/usr/bin/env bash
args=()
while (( $# )); do
    case "$1" in -e) shift 2; continue ;; esac
    args+=("${1#*@fake:}"); shift
done
exec /usr/bin/rsync "${args[@]}"
'''


def _exe(path: Path, text: str) -> None:
    path.write_text(text)
    path.chmod(0o755)


@pytest.fixture()
def cloud(tmp_path: Path):
    repo, remote, bindir = tmp_path / 'repo', tmp_path / 'remote', tmp_path / 'bin'
    for part in ('bench/lib', 'bench/cloud'):
        shutil.copytree(ROOT / part, repo / part, ignore=shutil.ignore_patterns('__pycache__'))
    cloud_dir = repo / 'bench/cloud'
    (cloud_dir / 'config.sh').write_text(CONFIG)
    _exe(cloud_dir / 'up.sh', UP)
    _exe(cloud_dir / 'install.sh', INSTALL)
    _exe(cloud_dir / 'down.sh', DOWN)
    (cloud_dir / 'jobs/fake.sh').write_text(JOB)
    shutil.copytree(ROOT / 'bench/lib', remote / 'bench/lib')
    bindir.mkdir()
    _exe(bindir / 'ssh', SSH)
    _exe(bindir / 'scp', SCP)
    _exe(bindir / 'rsync', RSYNC)
    subprocess.run(['git', 'init', '-q', str(repo)], check=True)
    env = {k: v for k, v in os.environ.items() if not k.startswith('BENCH_')}
    env.update({
        'PATH': f'{bindir}:{env["PATH"]}', 'REMOTE_REPO': str(remote),
        'CALLS': str(tmp_path / 'calls'), 'FAKE_SSH_LOG': str(tmp_path / 'ssh.log'),
        'FAKE_SSH_FAILURES': str(tmp_path / 'ssh-failures'),
        'CLOUD_POLL_INTERVAL': '0.2', 'FAKE_SLEEP': '0', 'FAKE_RC': '0',
    })
    return repo, env, tmp_path


def _session(cloud, *args: str, **env: str) -> subprocess.CompletedProcess[str]:
    repo, base, _ = cloud
    return subprocess.run(['bash', 'bench/cloud/session.sh', 'fake', *args], cwd=repo,
                          env={**base, **env}, capture_output=True, text=True, timeout=60)


def _calls(cloud) -> list[str]:
    path = cloud[2] / 'calls'
    return path.read_text().split('\n')[:-1] if path.exists() else []


def _run_dir(cloud) -> Path:
    run, = (cloud[0] / 'bench/results/cloud_fake').iterdir()
    return run


def test_a_completed_session_pulls_its_results_and_proves_teardown(cloud):
    done = _session(cloud)
    assert done.returncode == 0, done.stdout + done.stderr

    run = _run_dir(cloud)
    assert (run / 'result.txt').read_text() == 'done\n'
    assert json.loads((run / 'complete.json').read_text())['teardown_verified'] is True
    assert _calls(cloud) == ['up', 'install DEPLOY_GIT=0', 'down']
    assert not (cloud[0] / 'bench/.state/aws').exists()


def test_transient_ssh_failures_while_polling_do_not_end_the_session(cloud):
    (cloud[2] / 'ssh-failures').write_text('4')
    done = _session(cloud, FAKE_SLEEP='1')

    assert done.returncode == 0, done.stdout + done.stderr
    assert (_run_dir(cloud) / 'result.txt').exists()


def test_a_failed_runner_still_pulls_partial_results_then_tears_down(cloud):
    done = _session(cloud, FAKE_RC='7')

    assert done.returncode == 1
    run = _run_dir(cloud)
    assert (run / 'partial.txt').exists()
    assert not (run / 'complete.json').exists()
    assert _calls(cloud)[-1] == 'down'


def test_a_run_still_measuring_at_the_limit_keeps_the_instance_and_can_be_finished(cloud):
    done = _session(cloud, FAKE_SLEEP='4', FAKE_WAIT='1')

    assert done.returncode == 2, done.stdout + done.stderr
    assert 'down' not in _calls(cloud)
    assert (_run_dir(cloud) / 'partial.txt').exists()
    assert 'session.sh fake finish' in done.stdout
    assert (cloud[0] / 'bench/.state/aws').exists()

    time.sleep(4)
    finished = _session(cloud, 'finish')
    assert finished.returncode == 0, finished.stdout + finished.stderr
    assert (_run_dir(cloud) / 'result.txt').exists()
    assert _calls(cloud)[-1] == 'down'


def test_an_unverified_teardown_is_not_reported_complete(cloud):
    done = _session(cloud, FAKE_DOWN_FAILS='1')

    assert done.returncode != 0
    assert not (_run_dir(cloud) / 'complete.json').exists()


def test_an_existing_harness_state_is_never_overwritten(cloud):
    state = cloud[0] / 'bench/.state/aws'
    state.parent.mkdir(parents=True)
    state.write_text('SERVER_PUBLIC_IP=other\n')
    done = _session(cloud)

    assert done.returncode != 0
    assert _calls(cloud) == []
    assert state.read_text() == 'SERVER_PUBLIC_IP=other\n'


def test_the_instance_shuts_itself_down_after_the_session_could_have_ended(cloud):
    _session(cloud, FAKE_WAIT='3600')
    arm = next(line for line in (cloud[2] / 'ssh.log').read_text().splitlines()
               if 'shutdown -h' in line)

    minutes = int(arm.split('shutdown -h +')[1].split()[0])
    assert minutes > 60


def test_git_history_deploys_from_a_worktree(tmp_path):
    main, wt, remote, bindir = (tmp_path / n for n in ('main', 'wt', 'remote', 'bin'))
    bindir.mkdir()
    _exe(bindir / 'ssh', SSH)
    _exe(bindir / 'scp', SCP)
    git = ['git', '-c', 'user.email=f@example.invalid', '-c', 'user.name=f']
    subprocess.run(['git', 'init', '-q', str(main)], check=True)
    for i in (1, 2):
        (main / 'f.txt').write_text(f'{i}\n')
        subprocess.run([*git, '-C', str(main), 'add', '-A'], check=True)
        subprocess.run([*git, '-C', str(main), 'commit', '-qm', str(i)], check=True)
    subprocess.run(['git', '-C', str(main), 'worktree', 'add', '-q', '-b', 'w', str(wt)], check=True)
    shutil.copytree(wt, remote, ignore=shutil.ignore_patterns('.git'))
    env = {**os.environ, 'PATH': f'{bindir}:{os.environ["PATH"]}',
           'FAKE_SSH_LOG': str(tmp_path / 'ssh.log'), 'FAKE_SSH_FAILURES': str(tmp_path / 'none')}
    script = (f'SSH_OPTS=(); REMOTE_REPO={remote}; REPO_ROOT={wt}; '
              f'source {ROOT}/bench/cloud/remote.sh && remote_deploy_git tester@fake')

    done = subprocess.run(['bash', '-c', script], cwd=wt, env=env, capture_output=True,
                          text=True, timeout=60)
    assert done.returncode == 0, done.stdout + done.stderr

    head = subprocess.run(['git', '-C', str(wt), 'rev-parse', 'HEAD'], capture_output=True,
                          text=True, check=True).stdout.strip()
    rgit = ['git', '-C', str(remote)]
    assert subprocess.run([*rgit, 'rev-parse', 'HEAD'], capture_output=True,
                          text=True).stdout.strip() == head
    assert subprocess.run([*rgit, 'cat-file', '-e', 'HEAD~1^{commit}']).returncode == 0
    assert subprocess.run([*rgit, 'status', '--porcelain'], capture_output=True,
                          text=True).stdout == ''
