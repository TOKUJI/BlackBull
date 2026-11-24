"""The cloud jobs' local decisions: what they refuse, what they deploy, how long they wait."""
from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    """Three commits, each changing blackbull/ and bench/, so refs resolve
    without this repository's history (CI checks out one commit)."""
    git = ['git', '-c', 'user.email=f@example.invalid', '-c', 'user.name=f', '-C', str(tmp_path)]
    subprocess.run(['git', 'init', '-q', str(tmp_path)], check=True)
    for i in range(3):
        for part in ('blackbull', 'bench'):
            (tmp_path / part).mkdir(exist_ok=True)
            (tmp_path / part / 'x.py').write_text(f'X = {i}\n')
        subprocess.run([*git, 'add', '-A'], check=True)
        subprocess.run([*git, 'commit', '-qm', str(i)], check=True)
    return tmp_path


def _ab(repo: Path, script: str, **env: str) -> subprocess.CompletedProcess[str]:
    base = {k: v for k, v in os.environ.items()
            if not k.startswith(('REF_', 'ROUNDS', 'URL_PATH', 'H2_', 'WS_', 'PHASES', 'DURATION'))}
    job = ROOT / 'bench/cloud/jobs/ab.sh'
    return subprocess.run(
        ['bash', '-c', f'source {job} && job_preflight && {script}'],
        cwd=repo, env={**base, **env}, capture_output=True, text=True, timeout=30)


def test_the_documented_instance_type_is_the_default():
    doc = (ROOT / 'bench/ab/AB-HIGH-PRECISION.md').read_text()
    config = (ROOT / 'bench/cloud/config.sh').read_text()

    documented = set(re.findall(r'\bm7a\.\w+', doc))
    default = re.search(r': "\$\{INSTANCE_TYPE:=([\w.]+)\}"', config)[1]
    assert documented == {default}


def test_the_ab_job_deploys_git_and_resolves_refs_to_commits(repo):
    done = _ab(repo, 'echo "$JOB_DEPLOY_GIT $REF_BASE $REF_TREAT"', REF_BASE='HEAD~1', REF_TREAT='HEAD')
    assert done.returncode == 0, done.stderr

    deploy, base, treat = done.stdout.split()
    assert deploy == '1'
    assert re.fullmatch('[0-9a-f]{40}', base) and re.fullmatch('[0-9a-f]{40}', treat)


def test_the_ab_job_refuses_a_treatment_that_is_not_the_deployed_tree(repo):
    done = _ab(repo, 'true', REF_BASE='HEAD~2', REF_TREAT='HEAD~1', PATHSPEC='bench/')

    assert done.returncode != 0
    assert 'REF_TREAT' in done.stderr


def test_the_ab_wait_limit_grows_with_the_measurement(repo):
    def wait(**env: str) -> int:
        done = _ab(repo, 'echo "$JOB_WAIT_SECONDS"', REF_BASE='HEAD~1', REF_TREAT='HEAD', **env)
        assert done.returncode == 0, done.stderr
        return int(done.stdout)

    one_lane = wait(ROUNDS='8')
    assert wait(ROUNDS='24') > 2.5 * one_lane - 1200
    assert wait(ROUNDS='8', URL_PATHS='/conn,/plaintext', H2_PROFILES='/1kb') > 2 * one_lane - 1200
    # 8 rounds × 2 phases × 4 runs × (15 s + 5 s warm-up) is 21 minutes of wrk alone.
    assert one_lane > 21 * 60


def test_the_ab_runner_hands_the_h2_lanes_a_request_body(repo):
    done = _ab(repo, 'job_runner', REF_BASE='HEAD~1', REF_TREAT='HEAD', H2_PROFILES='/plaintext',
               H2_DATA='bench/ab/grpc/unary.bin', REMOTE_REPO='/r', REMOTE_RUN='/run')
    assert done.returncode == 0, done.stderr

    assert 'export H2_DATA=bench/ab/grpc/unary.bin' in done.stdout.splitlines()


def test_the_ab_runner_drives_one_gcannon_ws_lane_per_connection_count(repo):
    done = _ab(repo, 'job_runner', REF_BASE='HEAD~1', REF_TREAT='HEAD', URL_PATHS='',
               WS_CONNS='256,1024', WS_PATH='/ws-object', REMOTE_REPO='/r', REMOTE_RUN='/run')
    assert done.returncode == 0, done.stderr

    runs = [line for line in done.stdout.splitlines() if 'ab_commit_ws.sh' in line]
    assert len(runs) == 2
    for conns, run in zip(('256', '1024'), runs):
        assert f'BENCH_RUN_DIR=/run/ws-c{conns} ' in run
        assert f'WS_CONNS={conns} ' in run and 'WS_TOOL=gcannon ' in run
    assert 'export WS_PATH=/ws-object' in done.stdout.splitlines()
    assert 'ab_commit.sh' not in done.stdout


def test_the_ab_wait_limit_counts_the_ws_lanes(repo):
    def wait(**env: str) -> int:
        done = _ab(repo, 'echo "$JOB_WAIT_SECONDS"', REF_BASE='HEAD~1', REF_TREAT='HEAD', **env)
        assert done.returncode == 0, done.stderr
        return int(done.stdout)

    assert wait(URL_PATHS='', WS_CONNS='256,1024') > 2 * wait(URL_PATHS='', WS_CONNS='256') - 1200


def test_the_httparena_runner_pulls_back_only_the_results(tmp_path):
    """The HttpArena clone also carries the upstream site's logs (hundreds of MB)."""
    home, repo, run = tmp_path / 'home', tmp_path / 'repo', tmp_path / 'run'
    arena = home / 'HttpArena'
    for path, text in {'results/baseline/512/blackbull.json': '{"rps": 1}',
                       'site/static/logs/baseline/512/peer.log': 'x',
                       'frameworks/blackbull/meta.json': '{}'}.items():
        (arena / path).parent.mkdir(parents=True, exist_ok=True)
        (arena / path).write_text(text)
    (repo / 'bench/httparena').mkdir(parents=True)
    (repo / 'bench/httparena/run_httparena.sh').write_text('exit 0\n')
    run.mkdir()
    job = ROOT / 'bench/cloud/jobs/httparena.sh'
    env = {**os.environ, 'HOME': str(home), 'REMOTE_REPO': str(repo), 'REMOTE_RUN': str(run),
           'FRAMEWORKS': 'blackbull', 'PROFILES': 'baseline', 'SKIP_VALIDATE': '0'}
    runner = subprocess.run(['bash', '-c', f'source {job} && job_runner'], env=env,
                            capture_output=True, text=True, timeout=30, check=True).stdout
    done = subprocess.run(['bash', '-c', runner], env=env, capture_output=True, text=True, timeout=30)
    assert done.returncode == 0, done.stderr

    pulled = sorted(str(p.relative_to(run)) for p in run.rglob('*') if p.is_file())
    assert pulled == ['httparena-tree/results/baseline/512/blackbull.json']
