"""Tests reach BlackBull through public seams; private reach is a shrinking allowance.

Counted per test file: ``x._name`` on anything but ``self``/``cls``, and imports
of private names or modules from ``blackbull``.  A file may not exceed its
allowance in ``private_access_allowlist.json``, and an allowance that is no
longer used must be lowered, so the list only shrinks.
"""
import ast
import json
import pathlib

TESTS = pathlib.Path(__file__).resolve().parents[1]
ALLOWLIST = pathlib.Path(__file__).with_name('private_access_allowlist.json')


def _private(name: str) -> bool:
    return name.startswith('_') and not name.startswith('__')


def private_references(source: str) -> int:
    count = 0
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Attribute) and _private(node.attr):
            count += not (isinstance(node.value, ast.Name) and node.value.id in ('self', 'cls'))
        elif isinstance(node, ast.ImportFrom) and (node.module or '').startswith('blackbull'):
            count += any(_private(part) for part in node.module.split('.'))
            count += sum(_private(alias.name) for alias in node.names)
        elif isinstance(node, ast.Import):
            count += sum(any(_private(p) for p in a.name.split('.'))
                         for a in node.names if a.name.startswith('blackbull'))
    return count


def test_private_reach_stays_within_its_shrinking_allowance():
    allowed = json.loads(ALLOWLIST.read_text())
    actual = {}
    for path in sorted(TESTS.rglob('*.py')):
        if n := private_references(path.read_text()):
            actual[path.relative_to(TESTS).as_posix()] = n
    grown = {f: (n, allowed.get(f, 0)) for f, n in actual.items() if n > allowed.get(f, 0)}
    stale = {f: (actual.get(f, 0), n) for f, n in allowed.items() if actual.get(f, 0) < n}
    assert not grown, f'private reach beyond the allowance (now, allowed): {grown}'
    assert not stale, f'lower these allowances to what is used (now, allowed): {stale}'
