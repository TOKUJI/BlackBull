"""Every deprecation is one registry entry, warned about one way, and listed once.

``docs/reference/deprecations.md`` is compared with the registry here, so the
published list cannot drift from the code.
"""
import ast
import importlib
import pathlib
import sys
import warnings

import pytest

import blackbull
import blackbull.client
import blackbull.middleware
from blackbull import BlackBullDeprecationWarning
from blackbull.deprecations import DEPRECATIONS, table
from blackbull.fault_injection import scenario_h1, scenario_h1_server

ROOT = pathlib.Path(blackbull.__file__).parent
PAGE = ROOT.parent / 'docs' / 'reference' / 'deprecations.md'
_WARNING_CATEGORIES = {'DeprecationWarning', 'PendingDeprecationWarning', 'FutureWarning',
                       'BlackBullDeprecationWarning'}


def _calls():
    for path in sorted(ROOT.rglob('*.py')):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Call):
                func = node.func
                name = func.attr if isinstance(func, ast.Attribute) else getattr(func, 'id', None)
                yield path, node, name


def test_no_deprecation_warning_is_raised_outside_the_registry():
    stray = [f'{path.relative_to(ROOT.parent)}:{node.lineno}'
             for path, node, name in _calls()
             if name == 'warn' and path.name != 'deprecations.py'
             and any(isinstance(n, (ast.Name, ast.Attribute))
                     and (n.id if isinstance(n, ast.Name) else n.attr) in _WARNING_CATEGORIES
                     for arg in [*node.args, *(k.value for k in node.keywords)]
                     for n in ast.walk(arg))]
    assert stray == [], 'warn through blackbull.deprecations._warn_deprecated'


def test_every_warning_entry_is_used_and_every_use_is_an_entry():
    used = {node.args[0].value for _path, node, name in _calls()
            if name == '_warn_deprecated' and node.args
            and isinstance(node.args[0], ast.Constant)}
    assert used == {key for key, entry in DEPRECATIONS.items() if entry.warns}


_CLOSING = ('The table is generated from `blackbull/deprecations.py`; a test fails when\n'
            'the two differ.\n')


def test_the_published_list_is_the_registry():
    # Everything after the heading, so no row Markdown would render can slip in.
    section = PAGE.read_text().split('## Current deprecations\n', 1)[1]
    assert section == f'\n{table()}\n\n{_CLOSING}', (
        f'after its heading, {PAGE.relative_to(ROOT.parent)} must hold exactly '
        f'what blackbull.deprecations.table() returns and the closing sentence:\n{table()}')


def _fresh_import(name):
    sys.modules.pop(name, None)
    return importlib.import_module(name)


#: entry -> an access that warns, and whether the warning names the caller's line.
TRIGGERS = {
    'BBD001': (lambda: blackbull.middleware.CacheMiddleware, True),
    'BBD002': (lambda: blackbull.middleware.compress, True),
    'BBD003': (lambda: _fresh_import('blackbull.client.scenario'), False),
    'BBD004': (lambda: blackbull.Request, True),
    'BBD005': (lambda: blackbull.client.SendBytes, True),
    # beartype may wrap from_json, which moves a fixed stacklevel off the caller.
    'BBD006': (lambda: scenario_h1.Scenario.from_json('{"op": "ABORT"}'), False),
    'BBD007': (lambda: scenario_h1_server.encode_chunk, True),
}


def test_every_warning_entry_has_a_trigger_here():
    assert set(TRIGGERS) == {key for key, entry in DEPRECATIONS.items() if entry.warns}


@pytest.mark.parametrize('key', sorted(TRIGGERS))
def test_a_warning_names_its_entry_the_replacement_and_the_removal(key):
    access, at_caller = TRIGGERS[key]
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter('always')
        access()
    [warning] = [w for w in caught if issubclass(w.category, DeprecationWarning)]
    entry = DEPRECATIONS[key]
    assert warning.category is BlackBullDeprecationWarning
    message = str(warning.message)
    assert f'({key})' in message and entry.removal in message
    if at_caller:
        assert warning.filename == __file__
