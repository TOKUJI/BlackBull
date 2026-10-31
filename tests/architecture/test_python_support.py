"""The supported Python versions are stated once, by the pyproject.toml classifiers.

The floor, the type checker's target, the CI matrix and the published table
must all agree with them.
"""
import pathlib
import re
import tomllib

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[2]
PYPROJECT = tomllib.loads((ROOT / 'pyproject.toml').read_text())
PAGE = ROOT / 'docs' / 'reference' / 'python-support.md'
SUPPORTED = sorted(
    (m.group(1) for c in PYPROJECT['project']['classifiers']
     if (m := re.fullmatch(r'Programming Language :: Python :: (3\.\d+)', c))),
    key=lambda v: int(v.split('.')[1]))


def test_the_supported_versions_are_contiguous():
    minors = [int(v.split('.')[1]) for v in SUPPORTED]
    assert minors == list(range(minors[0], minors[-1] + 1)), SUPPORTED


def test_the_floor_is_the_oldest_supported_version():
    assert PYPROJECT['project']['requires-python'] == f'>={SUPPORTED[0]}'
    assert PYPROJECT['tool']['pyright']['pythonVersion'] == SUPPORTED[0]


def test_ci_tests_exactly_the_supported_versions():
    workflow = yaml.safe_load((ROOT / '.github' / 'workflows' / 'test.yml').read_text())
    assert workflow['jobs']['fast']['strategy']['matrix']['python-version'] == SUPPORTED


def test_the_published_table_lists_exactly_the_supported_versions():
    listed = re.findall(r'^\| (3\.\d+) \|', PAGE.read_text(), flags=re.MULTILINE)
    assert listed == SUPPORTED
