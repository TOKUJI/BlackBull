"""The public-API gate: what it reports between two commits, and when a release tag fails."""
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]
PACKAGES = ['subject', 'subject.client']

BASE = {
    'subject/__init__.py': "from .app import App, make\nfrom . import helpers\n__all__ = ['App', 'make', 'helpers']\n",
    'subject/app.py': (
        'class App:\n'
        '    def __init__(self, port=0):\n'
        '        self.port = port\n'
        '    def run(self, host):\n'
        '        return host\n'
        'def make(name, *, debug=False):\n'
        '    return App()\n'),
    'subject/helpers.py': "__all__ = ['assist']\ndef assist(x):\n    return x\ndef other(y):\n    return y\n",
    'subject/client/__init__.py': "from .conn import Conn\n__all__ = ['Conn']\n",
    'subject/client/conn.py': 'class Conn:\n    def send(self, data):\n        return data\n',
    'subject/internal.py': 'def engine(a, b):\n    return a + b\n',
    'pyproject.toml': '[tool.blackbull]\npublic-packages = ["subject", "subject.client"]\n',
}


def git(repo, *args):
    return subprocess.run(['git', *args], cwd=repo, check=True, capture_output=True,
                          text=True, timeout=30).stdout


def commit(repo, files, tag=None):
    for name, text in files.items():
        path = repo / name
        if text is None:
            path.unlink()
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    git(repo, 'add', '-A')
    git(repo, '-c', 'user.name=t', '-c', 'user.email=t@t', 'commit', '-q', '-m', 'c')
    if tag:
        git(repo, '-c', 'user.name=t', '-c', 'user.email=t@t', 'tag', '-a', tag, '-m', tag)
    return git(repo, 'rev-parse', 'HEAD').strip()


@pytest.fixture
def repo(tmp_path):
    git(tmp_path, 'init', '-q')
    commit(tmp_path, BASE, tag='v0.1.0')
    return tmp_path


def api(repo, *args, declared=False):
    packages = [] if declared else [arg for name in PACKAGES for arg in ('--package', name)]
    return subprocess.run(
        [sys.executable, '-m', 'tools.api_surface', '--repo', str(repo), *packages, *args],
        cwd=ROOT, capture_output=True, text=True, timeout=120)


def diff(repo, changes):
    commit(repo, changes)
    return api(repo, 'diff', 'v0.1.0', 'HEAD')


@pytest.mark.parametrize('changes, reported', [
    ({'subject/client/__init__.py': "__all__ = []\n"}, 'subject.client.Conn'),
    ({'subject/app.py': BASE['subject/app.py'].replace('def run(self, host)', 'def run(self)')},
     'subject.App'),
    ({'subject/app.py': BASE['subject/app.py'].replace('debug=False', 'debug=True')},
     'subject.make'),
    ({'subject/client/conn.py': 'class Conn:\n    pass\n'}, 'subject.client.Conn'),
    ({'subject/helpers.py': "__all__ = []\ndef other(y):\n    return y\n"},
     'subject.helpers.assist'),
], ids=['removed export', 'removed parameter', 'changed default', 'removed method',
        'removed from an exported module'])
def test_a_break_in_the_public_surface_fails_and_names_the_object(repo, changes, reported):
    result = diff(repo, changes)

    assert result.returncode == 1, result.stdout + result.stderr
    assert reported in result.stdout


@pytest.mark.parametrize('changes', [
    {'subject/internal.py': 'def engine(a):\n    return a\n'},
    {'subject/helpers.py': BASE['subject/helpers.py'].replace('def other(y)', 'def other()')},
    {'subject/client/__init__.py': "from .conn import Conn\nclass New:\n    pass\n__all__ = ['Conn', 'New']\n"},
    {'subject/app.py': BASE['subject/app.py'].replace('def run(self, host)', 'def run(self, host=None)')},
], ids=['internal module', 'unexported name of an exported module', 'added export',
        'compatible signature'])
def test_changes_outside_the_public_surface_or_compatible_ones_pass(repo, changes):
    result = diff(repo, changes)

    assert result.returncode == 0, result.stdout + result.stderr


def test_a_name_kept_as_a_deprecated_alias_passes(repo):
    result = diff(repo, {
        'subject/client/conn.py': (
            'import warnings\n'
            'class Connection:\n'
            '    def send(self, data):\n'
            '        return data\n'
            'class Conn(Connection):\n'
            '    def __init__(self):\n'
            "        warnings.warn('use Connection', DeprecationWarning)\n"),
        'subject/client/__init__.py': "from .conn import Conn, Connection\n__all__ = ['Conn', 'Connection']\n",
    })

    assert result.returncode == 0, result.stdout + result.stderr


BREAK = {'subject/client/__init__.py': "__all__ = []\n"}


@pytest.mark.parametrize('tag, changes, fails', [
    ('v0.1.1', BREAK, True),
    ('v0.2.0', BREAK, False),
    ('v1.0.0', BREAK, False),
    ('v0.1.1', {'subject/internal.py': 'def engine(a):\n    return a\n'}, False),
])
def test_a_release_tag_must_raise_minor_over_a_break(repo, tag, changes, fails):
    commit(repo, changes, tag=tag)

    result = api(repo, 'release', tag)

    assert result.returncode == int(fails), result.stdout + result.stderr
    if fails:
        assert 'subject.client.Conn' in result.stdout
        assert 'v0.1.0' in result.stdout


def test_the_public_packages_are_declared_once_in_pyproject():
    """With no --package, the gate reads the declaration the docs page uses."""
    from tools.api_surface import declaration

    packages = declaration((ROOT / 'pyproject.toml').read_text())

    assert packages[0] == 'blackbull'
    assert 'blackbull.client' in packages


def test_the_declaration_is_read_from_the_refs_and_narrowing_it_is_a_break(repo):
    """A change cannot exempt itself by editing the declaration it is judged by."""
    commit(repo, {'pyproject.toml': '[tool.blackbull]\npublic-packages = ["subject"]\n',
                  'subject/client/__init__.py': "__all__ = []\n"})

    result = api(repo, 'diff', 'v0.1.0', 'HEAD', declared=True)

    assert result.returncode == 1, result.stdout + result.stderr
    assert 'subject.client: no longer declared public' in result.stdout
    assert 'subject.client.Conn' in result.stdout


def test_an_error_is_not_mistaken_for_a_break(repo):
    result = api(repo, 'diff', 'no-such-ref', 'HEAD')

    assert result.returncode == 2
    assert 'no-such-ref' in result.stdout
