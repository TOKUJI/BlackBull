"""A child's ``import blackbull`` resolves to the tree under test (BLA-448)."""
from __future__ import annotations

import os
import pathlib
import shutil
import subprocess
import sys

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]


def test_a_foreign_pythonpath_entry_cannot_answer_for_the_child(
        tmp_path, child_env):
    fake = tmp_path / 'foreign'
    fake.mkdir()
    (fake / 'blackbull.py').write_text("marker = 'foreign'\n")
    env = child_env({'PYTHONPATH': str(fake)})
    probe = subprocess.run(
        [sys.executable, '-c', 'import blackbull; print(blackbull.__file__)'],
        env=env, capture_output=True, text=True, timeout=30)
    resolved = pathlib.Path((probe.stdout or '').strip()).resolve()
    assert resolved.is_relative_to(REPO_ROOT)
    assert 'foreign' not in resolved.parts


def test_the_blackbull_executable_comes_from_the_harness(child_env):
    """The executable a spawn finds is harness-provided, not an install."""
    env = child_env()
    found = shutil.which('blackbull', path=env['PATH'])
    assert found, 'the harness must put blackbull on the child PATH'
    preexisting = os.environ.get('PATH', '').split(os.pathsep)
    assert str(pathlib.Path(found).parent) not in preexisting, (
        f'{found} comes from a pre-existing install, not the harness')


def test_the_probe_uses_the_spawn_cwd(tmp_path, child_env):
    """The probe models the spawn's cwd; a shadowing claim fails loudly."""
    poisoned = tmp_path / 'poisoned'
    poisoned.mkdir()
    (poisoned / 'blackbull.py').write_text("marker = 'foreign'\n")
    with pytest.raises(AssertionError):
        child_env(cwd=poisoned)
    clean = tmp_path / 'clean'
    clean.mkdir()
    env = child_env(cwd=clean)
    probe = subprocess.run(
        [sys.executable, '-c', 'import blackbull; print(blackbull.__file__)'],
        env=env, cwd=clean, capture_output=True, text=True, timeout=30)
    resolved = pathlib.Path((probe.stdout or '').strip()).resolve()
    assert resolved.is_relative_to(REPO_ROOT)


def test_a_path_override_cannot_discard_the_shim(child_env):
    """The pins survive a caller overriding PATH or the tree root."""
    env = child_env({'PATH': '/usr/bin:/bin',
                     'BB_TEST_TREE_ROOT': '/somewhere/else'})
    found = shutil.which('blackbull', path=env['PATH'])
    assert found, 'the shim must survive a PATH override'
    assert str(pathlib.Path(found).parent) not in ('/usr/bin', '/bin'), (
        f'{found} came from the overridden PATH')
    assert env['BB_TEST_TREE_ROOT'] == str(REPO_ROOT)
