"""Architecture guard — keep BlackBull runnable on its declared Python floor.

``pyproject.toml`` declares ``requires-python = ">=3.11"``.  Some newer
stdlib APIs are attribute/method additions on *existing* types, so they
pass a plain import on the (newer) dev interpreter yet crash at runtime on
3.11.  The one that bit us: ``HTTPStatus.is_client_error`` /
``.is_server_error`` are 3.12-only, and using them made ``BlackBull()``
raise ``AttributeError`` on 3.11 (blackbull-requires-python-mismatch).

These grep-guards fail if a 3.12+-only attribute re-enters the package
source, so the mismatch can't silently come back.  Replacements live in
``blackbull.utils`` (``is_client_error`` / ``is_server_error``).
"""
import pathlib

import blackbull

PKG_ROOT = pathlib.Path(blackbull.__file__).parent

# (attribute fragment, first CPython version, 3.11-safe replacement)
_FORBIDDEN_ATTRS = [
    ('.is_client_error', '3.12', 'blackbull.utils.is_client_error(status)'),
    ('.is_server_error', '3.12', 'blackbull.utils.is_server_error(status)'),
    ('.is_informational', '3.12', 'an int-range check'),
    ('.is_redirection', '3.12', 'an int-range check'),
]


def _package_sources():
    for p in PKG_ROOT.rglob('*.py'):
        yield p, p.read_text(encoding='utf-8')


def test_no_312_only_httpstatus_category_properties():
    offenders: list[str] = []
    for path, src in _package_sources():
        # Skip the utils module: its docstring/comments *name* these
        # attributes to explain the replacement — it never calls them.
        if path.name == 'utils.py':
            continue
        for frag, ver, repl in _FORBIDDEN_ATTRS:
            if frag in src:
                rel = path.relative_to(PKG_ROOT.parent)
                offenders.append(
                    f'{rel}: uses `{frag}` (added in Python {ver}; '
                    f'breaks the >=3.11 floor) — use {repl}')
    assert not offenders, (
        'Python-floor violation(s):\n  ' + '\n  '.join(offenders))
