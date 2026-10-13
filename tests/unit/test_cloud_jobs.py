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
            if not k.startswith(('REF_', 'ROUNDS', 'URL_PATH', 'H2_', 'PHASES', 'DURATION'))}
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
