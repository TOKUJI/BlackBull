"""Every default this tree states in prose is the default it ships.

Three surfaces state a default for the same knob, and each can drift on its
own: the ``Settings`` dataclass field, the ``blackbull.env`` module docstring,
and ``docs/reference/env-vars.md``.  Only :func:`get_settings` is *executed*,
so the other three are documentation and a reader has no way to tell a stale
one from a live one.  These tests make the drift a test failure instead of a
discovery.

The comparison is against the **expression** ``get_settings`` evaluates, not
against a number captured when the test was written: a default derived from
``os.cpu_count()`` is a different integer on every box, and a test that froze
one would fail on the next machine while telling nobody anything true.
"""
from __future__ import annotations

import ast
import os
import pathlib
import re

import blackbull
import blackbull.env as env_module

ENV_PY = pathlib.Path(env_module.__file__)
ENV_VARS_MD = pathlib.Path(__file__).resolve().parents[2] / 'docs' / 'reference' / 'env-vars.md'

#: Variables the module docstring and ``env-vars.md`` describe that
#: :func:`get_settings` does not read, with the reader that does read each.
#: Listed rather than skipped: a name that stops being read anywhere should
#: fail here, not quietly become fiction in two documents.
_VAR = re.compile(r'^(?:BB|BLACKBULL)_[A-Z0-9_]*$')


def _read_elsewhere() -> set[str]:
    """Variables read straight off ``os.environ``, anywhere in the package.

    Not every knob goes through ``Settings`` -- the warm-up budget, the
    deadline tick, the gRPC message ceiling and the phase tracer read the
    environment where they are used.  Found by scanning rather than listed by
    hand: a list is one more thing to keep true, and it fell six behind.
    """
    root = pathlib.Path(__file__).resolve().parents[2] / 'blackbull'
    found: set[str] = set()
    for path in root.rglob('*.py'):
        for node in ast.walk(ast.parse(path.read_text(encoding='utf-8'))):
            if isinstance(node, ast.Call) and node.args:
                first = node.args[0]
                if (isinstance(first, ast.Constant) and isinstance(first.value, str)
                        and _VAR.match(first.value)):
                    found.add(first.value)
            elif (isinstance(node, ast.Subscript)
                  and isinstance(node.slice, ast.Constant)
                  and isinstance(node.slice.value, str)
                  and _VAR.match(node.slice.value)):
                found.add(node.slice.value)
    return found

#: ``BLACKBULL_ENV`` is read by ``get_settings`` before the ``Settings(...)``
#: call, to pick the :class:`Environment` member, so it carries no default
#: expression inside that call to compare against.
ENV_SELECTOR = 'BLACKBULL_ENV'

#: ``BB_MAX_CONNECTIONS`` passes through :func:`resolve_max_connections`
#: instead of a ``_*_env`` reader: its default is the word ``auto``, which
#: resolves from this process's ``RLIMIT_NOFILE`` at call time.  No static
#: value can equal it, so the documented literal is the word itself.
DERIVED_DEFAULT_LITERALS = {'BB_MAX_CONNECTIONS': 'auto'}

#: ``Settings`` fields whose dataclass default cannot equal what
#: ``get_settings`` passes, with the reason it cannot.
FIELD_DEFAULT_EXEMPT = {
    'env': 'an Environment member chosen before the Settings(...) call',
    'max_connections': 'resolved from RLIMIT_NOFILE by resolve_max_connections',
}


def _env_ast() -> ast.Module:
    return ast.parse(ENV_PY.read_text(), str(ENV_PY))


def _settings_call(tree: ast.Module) -> ast.Call:
    fn = next(n for n in tree.body
              if isinstance(n, ast.FunctionDef) and n.name == 'get_settings')
    return next(n for n in ast.walk(fn)
                if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                and n.func.id == 'Settings')


def _field_defaults(tree: ast.Module) -> dict[str, str]:
    cls = next(n for n in tree.body
               if isinstance(n, ast.ClassDef) and n.name == 'Settings')
    return {st.target.id: ast.unparse(st.value)
            for st in cls.body
            if isinstance(st, ast.AnnAssign) and isinstance(st.target, ast.Name)
            and st.value is not None}


def _keyword_defaults(call: ast.Call) -> dict[str, str]:
    """field name -> the expression ``get_settings`` passes as its default."""
    out: dict[str, str] = {}
    for kw in call.keywords:
        if kw.arg is None:
            continue
        for sub in ast.walk(kw.value):
            if (isinstance(sub, ast.Call) and isinstance(sub.func, ast.Name)
                    and sub.func.id.endswith('_env') or
                    isinstance(sub, ast.Call) and isinstance(sub.func, ast.Name)
                    and '_env' in sub.func.id):
                if len(sub.args) > 1:
                    out[kw.arg] = ast.unparse(sub.args[1])
                break
    return out


def _env_reads(call: ast.Call) -> dict[str, tuple[str, str | None]]:
    """env var -> (Settings field it feeds, default expression or None)."""
    out: dict[str, tuple[str, str | None]] = {}
    for kw in call.keywords:
        if kw.arg is None:
            continue
        for sub in ast.walk(kw.value):
            if not (isinstance(sub, ast.Call) and isinstance(sub.func, ast.Name)
                    and '_env' in sub.func.id and sub.args
                    and isinstance(sub.args[0], ast.Constant)
                    and isinstance(sub.args[0].value, str)):
                continue
            default = ast.unparse(sub.args[1]) if len(sub.args) > 1 else None
            out[sub.args[0].value] = (kw.arg, default)
            break
        else:
            # No ``_*_env`` reader — find the env-var name literal anyway
            # (``resolve_max_connections(os.environ.get('BB_MAX_CONNECTIONS'))``).
            for sub in ast.walk(kw.value):
                if (isinstance(sub, ast.Constant) and isinstance(sub.value, str)
                        and sub.value.startswith(('BB_', 'BLACKBULL_'))):
                    out[sub.value] = (kw.arg, None)
                    break
    return out


def _evaluate(expr: str):
    """Evaluate a default expression against ``blackbull.env``'s own globals."""
    return eval(expr, {'os': os, **vars(env_module)})  # noqa: S307


def _spellings(value) -> set[str]:
    """The literal spellings that faithfully denote ``value`` in prose."""
    if isinstance(value, bool):
        return {'1', 'true'} if value else {'0', 'false'}
    if isinstance(value, float):
        out = {repr(value)}
        if value.is_integer():
            out.add(str(int(value)))
        return out
    if isinstance(value, int):
        return {str(value)}
    if isinstance(value, str):
        return {value}
    return {str(value)}


def _states(claim: str, value) -> bool:
    """Does the documented literal ``claim`` denote ``value``?

    A claim may be an expression (``max((os.cpu_count() or 1) * 2, 4)``) as
    well as a literal; an expression is the honest form for a derived default
    and is the only form that stays true on a box with a different CPU count.
    """
    claim = claim.strip()
    try:
        if _evaluate(claim) == value and isinstance(_evaluate(claim), type(value)):
            return True
    except Exception:
        pass
    return claim.strip('`*').lower() in {s.lower() for s in _spellings(value)}


def _md_defaults() -> dict[str, str]:
    """env var -> the Default column of its first row in env-vars.md."""
    rows: dict[str, str] = {}
    for line in ENV_VARS_MD.read_text().splitlines():
        m = re.match(r'^\|\s*`(BB_[A-Z0-9_]+|BLACKBULL_ENV)`\s*\|\s*(.*?)\s*\|', line)
        if m and m.group(1) not in rows:
            rows[m.group(1)] = m.group(2)
    return rows


def _md_literal(cell: str) -> str | None:
    """The first `…`-quoted literal in a Default cell, or None if it has none.

    A cell with no code literal is stating the variable is unset — *(unset)*,
    *(plain)* — which is the only honest form for an empty default in a table
    of shell values, since ``FOO=`` and an absent ``FOO`` are different things
    and neither is spelled by a literal.
    """
    m = re.search(r'`(.*?)`', cell)
    return m.group(1).strip() if m else None


def _expected_literal(var: str, default_expr: str | None):
    """(value, spelling) a document must state for ``var``, or None to skip."""
    if var in DERIVED_DEFAULT_LITERALS:
        return DERIVED_DEFAULT_LITERALS[var]
    if default_expr is None:
        return None
    return _evaluate(default_expr)


# ---------------------------------------------------------------------------


def test_settings_field_defaults_match_get_settings():
    """A ``Settings`` field default is documentation; it must not lie.

    ``get_settings`` is the only construction site and passes every field
    explicitly, so a field default is never the operative value — which is
    exactly why a wrong one survives: nothing executes it.
    """
    tree = _env_ast()
    fields = _field_defaults(tree)
    passed = _keyword_defaults(_settings_call(tree))

    missing = sorted(set(fields) - set(passed) - set(FIELD_DEFAULT_EXEMPT))
    assert not missing, (
        'Settings fields get_settings passes no comparable default for, and '
        f'that are not in FIELD_DEFAULT_EXEMPT: {missing}.  Either give the '
        'field a reader with a default, or exempt it with a written reason.')

    drift = []
    for field, expr in sorted(passed.items()):
        if field in FIELD_DEFAULT_EXEMPT:
            continue
        declared = fields.get(field)
        if declared is None:
            continue
        if _evaluate(declared) != _evaluate(expr):
            drift.append(
                f'  Settings.{field}: dataclass default {declared} '
                f'(= {_evaluate(declared)!r}) but get_settings passes {expr} '
                f'(= {_evaluate(expr)!r})')
    assert not drift, (
        'Settings field defaults disagree with the values get_settings '
        'passes:\n' + '\n'.join(drift))


def test_package_docstring_states_the_import_side_effect_it_has():
    """``blackbull/__init__.py`` says the server stack loads; check that it does.

    This is the claim that drifted furthest — the docstring asserted the exact
    opposite for as long as nothing executed it.  The test does not require the
    coupling to stay: making the import lazy is a fine change, and this failing
    is how the sentence gets rewritten with it rather than a year later.
    """
    import subprocess
    import sys

    loaded = subprocess.run(
        [sys.executable, '-c',
         'import sys, blackbull; '
         "print(len([m for m in sys.modules if m.startswith('blackbull.server')]))"],
        capture_output=True, text=True, check=True).stdout.strip()

    doc = blackbull.__doc__ or ''
    claims_loaded = 'loads the server stack' in doc
    assert claims_loaded == (int(loaded) > 0), (
        f'`import blackbull` loads {loaded} blackbull.server.* modules, but '
        f'the package docstring {"claims it does" if claims_loaded else "does not say so"}.  '
        'Whichever of the two changed, change the other.')


def test_env_vars_md_documents_every_default():
    """``docs/reference/env-vars.md`` calls itself exhaustive; hold it to that.

    ``1``/``True`` and ``0``/``0.0`` are accepted as the same statement: the
    page documents the *environment* spelling, which is always a string, while
    the code holds a parsed ``bool``/``float``.  Requiring ``True`` in a table
    of shell values would make the page wrong for its own readers.
    """
    reads = _env_reads(_settings_call(_env_ast()))
    rows = _md_defaults()

    missing = sorted((set(reads) | _read_elsewhere()) - set(rows) - {ENV_SELECTOR})
    assert not missing, (
        'env vars blackbull/ reads with no row in docs/reference/'
        f'env-vars.md, which calls itself exhaustive: {missing}')

    extra = sorted(set(rows) - set(reads) - _read_elsewhere() - {ENV_SELECTOR})
    assert not extra, (
        'env-vars.md rows for variables nothing in blackbull/ reads.  Delete '
        f'the row: {extra}')

    drift = []
    for var, (field, expr) in sorted(reads.items()):
        if var == ENV_SELECTOR:
            continue
        expected = _expected_literal(var, expr)
        if expected is None:
            continue
        claim = _md_literal(rows[var])
        if claim is None:
            if expected != '':
                drift.append(f'  {var} (Settings.{field}): env-vars.md Default '
                             f'column names no value ({rows[var]!r}), but '
                             f'get_settings uses {expr} (= {expected!r})')
            continue
        if not _states(claim, expected):
            drift.append(f'  {var} (Settings.{field}): env-vars.md Default '
                         f'column says `{claim}`, get_settings uses '
                         f'{expr or "auto"} (= {expected!r})')
    assert not drift, (
        'docs/reference/env-vars.md Default columns disagree with the code:\n'
        + '\n'.join(drift))
