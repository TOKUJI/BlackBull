"""bench/lib/results gives every tool the same run directory, from Python or shell.

A run lives at ``<root>/<tool>/<UTC time>-<short rev>/`` with ``tmp/`` for the
run's temporary files and ``manifest.json`` recording what produced it.
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from bench.lib import results

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture()
def root(tmp_path, monkeypatch):
    monkeypatch.setenv('BENCH_RESULTS_ROOT', str(tmp_path))
    monkeypatch.delenv('BENCH_RUN_DIR', raising=False)
    return tmp_path


def _shell(script: str, root: Path, **env: str) -> list[str]:
    return subprocess.run(
        ['bash', '-c', f'source bench/lib/results.sh && {script}'],
        cwd=ROOT, env={**os.environ, 'BENCH_RESULTS_ROOT': str(root), **env},
        capture_output=True, text=True, timeout=30, check=True).stdout.split()


def test_a_run_directory_sits_under_its_tool_with_tmp_and_manifest(root):
    run = results.run_dir('ab_commit', settings={'rounds': 3})

    assert run.parent == root / 'ab_commit'
    assert (run / 'tmp').is_dir()
    manifest = json.loads((run / 'manifest.json').read_text())
    assert manifest['tool'] == 'ab_commit'
    assert manifest['settings'] == {'rounds': 3}
    assert manifest['revision']
    assert manifest['argv']


def test_two_runs_in_the_same_second_get_different_directories(root):
    assert results.run_dir('t') != results.run_dir('t')


def test_the_shell_helper_gives_the_same_layout(root):
    run, tmp, tmpdir = (Path(p) for p in _shell(
        'bench_run_dir probe rounds=3 && echo "$BENCH_RUN_DIR $BENCH_TMP $TMPDIR"', root))

    assert run.parent == root / 'probe'
    assert tmp == tmpdir == run / 'tmp' and tmp.is_dir()
    manifest = json.loads((run / 'manifest.json').read_text())
    assert manifest['tool'] == 'probe'
    assert manifest['settings'] == {'rounds': '3'}


def test_a_tool_started_by_another_joins_its_run(root):
    outer = results.run_dir('outer')
    joined = _shell('bench_run_dir inner && echo "$BENCH_RUN_DIR"', root,
                    BENCH_RUN_DIR=str(outer))

    assert joined == [str(outer)]
    assert not (root / 'inner').exists()


def test_a_caller_can_name_a_fresh_directory_for_each_child(root):
    outer = results.run_dir('outer')
    child = outer / 'batch_1'
    _shell('bench_run_dir inner cases=1.*', root, BENCH_RUN_DIR=str(child))

    assert (child / 'tmp').is_dir()
    assert json.loads((child / 'manifest.json').read_text())['settings'] == {'cases': '1.*'}


def test_the_latest_run_of_a_tool_is_found_without_naming_the_root(root):
    assert results.latest_run('probe') is None
    results.run_dir('probe')
    newest = results.run_dir('probe')

    (root / 'probe' / 'my-hand-made-run').mkdir()
    (root / 'probe' / 'zzz').mkdir()

    assert results.latest_run('probe') == newest
    assert _shell('bench_latest_run probe', root) == [str(newest)]


def test_joining_an_existing_directory_provides_its_tmp(root):
    existing = root / 'by-hand'
    existing.mkdir()
    tmp, = _shell('bench_run_dir x && echo "$TMPDIR"', root, BENCH_RUN_DIR=str(existing))

    assert Path(tmp) == existing / 'tmp' and Path(tmp).is_dir()


def test_the_default_root_is_bench_results(monkeypatch):
    monkeypatch.delenv('BENCH_RESULTS_ROOT', raising=False)
    assert results.results_root() == ROOT / 'bench' / 'results'
