"""Every environment setting is spelled once, where it is declared.

The declaration carries the name, type, default, range and description, and
everything else (the snapshot, run() resolution, the reference page) reads
it from there.  A second spelling of the name is a second place that can
drift from the first.
"""
import ast
import dataclasses
import os
import pathlib
import re
import subprocess
import sys

from blackbull.config import AppConfig
from blackbull.env import Settings

PACKAGE = pathlib.Path(__file__).resolve().parents[2] / 'blackbull'
ENV_NAME = re.compile(r'B(?:B|LACKBULL)_[A-Z0-9_]+')


def _declared() -> list[str]:
    return [f.metadata['env'] for cls in (Settings, AppConfig)
            for f in dataclasses.fields(cls) if 'env' in f.metadata]


def _spellings() -> dict[str, list[str]]:
    seen: dict[str, list[str]] = {}
    for path in PACKAGE.rglob('*.py'):
        for node in ast.walk(ast.parse(path.read_text(encoding='utf-8'))):
            if (isinstance(node, ast.Constant) and isinstance(node.value, str)
                    and ENV_NAME.fullmatch(node.value)):
                seen.setdefault(node.value, []).append(
                    f'{path.relative_to(PACKAGE.parent)}:{node.lineno}')
    return seen


def test_every_declared_setting_is_spelled_once():
    spellings = _spellings()
    repeated = {name: spellings.get(name, []) for name in _declared()
                if len(spellings.get(name, [])) != 1}
    assert not repeated, repeated


def test_no_environment_name_is_spelled_twice():
    repeated = {name: where for name, where in _spellings().items()
                if len(where) > 1}
    assert not repeated, repeated


def test_every_declared_name_is_unique():
    names = _declared()
    assert len(names) == len(set(names))


def test_settings_exported_after_import_are_still_read():
    """``blackbull run --config`` imports the package, then exports the file's
    values into the environment; a snapshot cached during import would hide
    them."""
    script = (
        'import os, blackbull, blackbull.grpc.asgi\n'
        "os.environ['BB_HEADER_TIMEOUT'] = '7'\n"
        'from blackbull.env import get_settings\n'
        'assert get_settings().header_timeout == 7.0, get_settings().header_timeout\n')
    env = {k: v for k, v in os.environ.items() if not k.startswith('BB_')}
    subprocess.run([sys.executable, '-c', script], check=True, env=env, timeout=60)
