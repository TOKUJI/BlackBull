"""The supported Python versions are stated once, by the pyproject.toml classifiers.

The floor, the type checker's target, the CI matrix and the published table
must all agree with them.
"""
import pathlib
import re
import tomllib

import markdown
import yaml

ROOT = pathlib.Path(__file__).resolve().parents[2]
PYPROJECT = tomllib.loads((ROOT / 'pyproject.toml').read_text())
PAGE = ROOT / 'docs' / 'reference' / 'python-support.md'
INSTALLATION = ROOT / 'docs' / 'getting-started' / 'installation.md'
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


def _render(text: str) -> str:
    """*text* as the site renders it: MkDocs' own extensions plus mkdocs.yml's."""
    names, configs = ['tables', 'fenced_code', 'toc'], {}
    for ext in yaml.safe_load((ROOT / 'mkdocs.yml').read_text())['markdown_extensions']:
        if isinstance(ext, dict):
            [(name, options)] = ext.items()
            configs[name] = options or {}
        else:
            name = ext
        names.append(name)
    return markdown.markdown(text, extensions=list(dict.fromkeys(names)),
                             extension_configs=configs)


def test_the_published_table_lists_exactly_the_supported_versions():
    # Every row the site would show counts, however it is spelled or nested.
    html = _render(PAGE.read_text())
    listed = re.findall(r'<tr>\s*<td>(.*?)</td>', html)
    assert listed == SUPPORTED


def test_the_installation_page_states_the_floor():
    assert re.findall(r'Python (3\.\d+)\+', INSTALLATION.read_text()) == [SUPPORTED[0]]
