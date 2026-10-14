"""Every bench tool is in the index, parses, and answers --help.

A tool nobody can find is a tool somebody rewrites, and a tool nobody runs
rots: bench/README.md names every tool by path, and each one is at least
parsed here (the tools themselves need servers, load generators or cloud
accounts, so they are not run for real).
"""
from __future__ import annotations

import os
import py_compile
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
INDEX = ROOT / 'bench' / 'README.md'
FILES = [f for f in subprocess.run(['git', 'ls-files', 'bench', 'scripts/run-*.sh'], cwd=ROOT,
                                   capture_output=True, text=True, check=True).stdout.split()
         if not f.endswith('__init__.py')]
TOOLS = {f for f in FILES
         if f.endswith(('.sh', '.py', '.js', '.lua', '.conf', '.toml', '.ini'))
         or Path(f).name.startswith('Dockerfile')}
SHELL = sorted(f for f in TOOLS if f.endswith('.sh'))
PYTHON = sorted(f for f in TOOLS if f.endswith('.py'))
#: Command lines: argparse tools, and wrappers that run one through runpy.
CLI = [f for f in PYTHON
       if re.search(r'\bargparse\b|runpy\.run_path', text := (ROOT / f).read_text())
       and "__name__ == '__main__'" in text.replace('"', "'")]
#: Bench modules imported by bare name: failing to find one is a bug, not a
#: missing optional dependency.
BENCH_MODULES = {Path(f).stem for f in PYTHON}


def _named() -> set[str]:
    return set(re.findall(r'`((?:bench|scripts)/[^`\s<]+)`', INDEX.read_text()))


def test_every_tool_is_in_the_index():
    assert TOOLS - _named() == set(), 'add the tool to bench/README.md'


def test_every_indexed_path_exists():
    missing = {p for p in _named() if not (ROOT / p.rstrip('/')).exists()}
    assert missing == set(), 'remove or correct these entries in bench/README.md'


@pytest.mark.parametrize('path', SHELL)
def test_shell_tools_parse(path):
    done = subprocess.run(['bash', '-n', path], cwd=ROOT, capture_output=True, text=True)
    assert done.returncode == 0, done.stderr


@pytest.mark.parametrize('path', PYTHON)
def test_python_tools_compile(path, tmp_path):
    py_compile.compile(str(ROOT / path), cfile=str(tmp_path / 'x.pyc'), doraise=True)


@pytest.mark.parametrize('path', CLI)
def test_python_tools_answer_help(path, tmp_path):
    env = {**os.environ, 'PYTHONPATH': str(ROOT), 'BENCH_RESULTS_ROOT': str(tmp_path)}
    env.pop('BENCH_RUN_DIR', None)
    done = subprocess.run([sys.executable, path, '--help'], cwd=ROOT, env=env,
                          capture_output=True, text=True, timeout=60)
    needs = re.search(r'needs Python (\d+)\.(\d+)\+', done.stderr)
    if needs and sys.version_info < (int(needs[1]), int(needs[2])):
        pytest.skip(f'{path} needs Python {needs[1]}.{needs[2]}+')
    missing = re.search(r"No module named '([\w.]+)'", done.stderr)
    if missing and missing[1].split('.')[0] not in {'blackbull', 'bench', *BENCH_MODULES}:
        pytest.skip(f'optional dependency {missing[1]} is not installed')
    assert done.returncode == 0, done.stderr
    assert 'usage' in done.stdout.lower()
