"""The shared A/B core (bench/ab/abba.sh), driven through a fake protocol.

A throwaway git repository holds a ``blackbull`` package whose ``VALUE``
differs between commits; the fake protocol's measurement imports it, so the
recorded numbers show which bytes each arm really ran.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]

FAKE_PROTOCOL = r'''#!/usr/bin/env bash
source "$(dirname "$0")/abba.sh"
AB_TOOL=fake_ab
AB_LANE='fake lane'
ab_start_server() { ab_spawn sleep 30; printf '%s\n' "$ARM_ENV" > "$BENCH_TMP/arm_env"; }
ab_measure() {
    if grep -q 'X=' "$BENCH_TMP/arm_env"; then
        sed -n 's/.*X=\([0-9]*\).*/\1/p' "$BENCH_TMP/arm_env"
    else
        "$PY" -c 'import blackbull.core as c; print(c.VALUE)'
    fi
}
abba_main
'''


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(['git', '-C', str(repo), *args], check=True,
                          capture_output=True, text=True).stdout.strip()


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    repo = tmp_path / 'repo'
    for part in ('bench/lib', 'bench/ab'):
        shutil.copytree(ROOT / part, repo / part, ignore=shutil.ignore_patterns('__pycache__'))
    (repo / 'bench/ab/fake.sh').write_text(FAKE_PROTOCOL)
    pkg = repo / 'blackbull'
    pkg.mkdir()
    (pkg / '__init__.py').write_text('')
    _git(tmp_path, 'init', '-q', str(repo))
    _git(repo, 'config', 'user.email', 'fixture@example.invalid')
    _git(repo, 'config', 'user.name', 'fixture')
    for value in (100, 97, 95):
        (pkg / 'core.py').write_text(f'VALUE = {value}\n')
        _git(repo, 'add', '-A')
        _git(repo, 'commit', '-qm', f'value {value}')
    return repo


def _run(repo: Path, **env: str) -> subprocess.CompletedProcess[str]:
    base = {k: v for k, v in os.environ.items()
            if not k.startswith(('BENCH_', 'REF_', 'PHASES', 'ROUNDS'))}
    return subprocess.run(
        ['bash', 'bench/ab/fake.sh'], cwd=repo, capture_output=True, text=True, timeout=60,
        env={**base, 'PY': sys.executable, 'SERVER_CPUS': '', 'LOAD_CPUS': '',
             'ROUNDS': '2', **env})


def _rows(run: Path) -> list[list[str]]:
    return [line.split('\t') for line in (run / 'raw.tsv').read_text().splitlines()[1:]]


def _only_run(repo: Path, tool: str = 'fake_ab') -> Path:
    run, = (repo / 'bench/results' / tool).iterdir()
    return run


def test_arms_run_their_own_bytes_in_abba_order_and_null_runs_the_treatment(repo):
    done = _run(repo, REF_BASE='HEAD~1', REF_TREAT='HEAD')
    assert done.returncode == 0, done.stdout + done.stderr

    rows = _rows(_only_run(repo))
    real = [(r[1], r[2], r[3]) for r in rows if r[0] == 'real']
    assert real == [('1', 'base', '97'), ('1', 'treat', '95'), ('1', 'treat', '95'),
                    ('1', 'base', '97'), ('2', 'treat', '95'), ('2', 'base', '97'),
                    ('2', 'base', '97'), ('2', 'treat', '95')]
    assert {r[3] for r in rows if r[0] == 'null'} == {'95'}
    assert _git(repo, 'status', '--porcelain', '--untracked-files=no') == ''


def test_a_run_leaves_report_and_manifest_in_its_run_directory(repo):
    _run(repo, REF_BASE='HEAD~1', REF_TREAT='HEAD', PHASES='real')
    run = _only_run(repo)

    assert '# A/B' in (run / 'report.md').read_text()
    settings = json.loads((run / 'manifest.json').read_text())['settings']
    assert settings['ref_treat'] == _git(repo, 'rev-parse', 'HEAD')


def test_a_named_run_directory_is_used(repo, tmp_path):
    lane = tmp_path / 'session' / 'lane-1'
    _run(repo, REF_BASE='HEAD~1', REF_TREAT='HEAD', PHASES='real', BENCH_RUN_DIR=str(lane))

    assert (lane / 'raw.tsv').exists() and (lane / 'report.md').exists()


def test_refuses_a_treatment_that_is_not_the_checked_out_tree(repo):
    done = _run(repo, REF_BASE='HEAD~2', REF_TREAT='HEAD~1')

    assert done.returncode != 0
    assert 'REF_TREAT' in done.stderr
    assert not (repo / 'bench/results/fake_ab').exists()


def test_refuses_uncommitted_changes_in_the_swapped_files(repo):
    (repo / 'blackbull/core.py').write_text('VALUE = 1\n')
    done = _run(repo, REF_BASE='HEAD~1', REF_TREAT='HEAD')

    assert done.returncode != 0
    assert 'uncommitted' in done.stderr
    assert (repo / 'blackbull/core.py').read_text() == 'VALUE = 1\n'


def test_identical_refs_compare_two_environments(repo):
    done = _run(repo, REF_BASE='HEAD', REF_TREAT='HEAD', PHASES='real',
                BASE_EXTRA_ENV='X=100', TREAT_EXTRA_ENV='X=97')
    assert done.returncode == 0, done.stdout + done.stderr

    rows = _rows(_only_run(repo))
    assert {(r[2], r[3]) for r in rows} == {('base', '100'), ('treat', '97')}
