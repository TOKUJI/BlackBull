"""bench/README.md is the index: every bench tool is named there, by path.

A tool nobody can find is a tool somebody rewrites.  The index names each
entry point and each part, so a question leads to one existing tool; this
test fails when a tool is added without an entry, or an entry outlives its
tool.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
INDEX = ROOT / 'bench' / 'README.md'
CODE = ('.sh', '.py', '.js', '.lua', '.conf', '.toml', '.ini')


def _tools() -> set[str]:
    files = subprocess.run(['git', 'ls-files', 'bench', 'scripts/run-*.sh'], cwd=ROOT,
                           capture_output=True, text=True, check=True).stdout.split()
    return {f for f in files
            if (f.endswith(CODE) or Path(f).name.startswith('Dockerfile'))
            and not f.endswith('__init__.py')}


def _named() -> set[str]:
    return set(re.findall(r'`((?:bench|scripts)/[^`\s<]+)`', INDEX.read_text()))


def test_every_tool_is_in_the_index():
    assert _tools() - _named() == set(), 'add the tool to bench/README.md'


def test_every_indexed_path_exists():
    missing = {p for p in _named() if not (ROOT / p.rstrip('/')).exists()}
    assert missing == set(), 'remove or correct these entries in bench/README.md'
