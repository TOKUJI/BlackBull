"""A child's ``import blackbull`` resolves to the tree under test (BLA-448)."""
from __future__ import annotations

import pathlib
import subprocess
import sys

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
