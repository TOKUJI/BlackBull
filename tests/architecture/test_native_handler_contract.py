"""Architecture guard: a registered handler receives a ``Connection``, not a scope.

BlackBull's own server threads a typed ``Connection`` end-to-end; an ASGI scope
dict exists only at the two external boundaries. A handler registered with
``@app.route(...)`` / ``@app.websocket(...)``, or a middleware installed via
``app.use(...)`` / ``middlewares=[...]``, is therefore handed a ``Connection``
on the native path.

Using that parameter as a mapping — ``conn['headers']``, ``conn.get(…)``,
``conn.setdefault('state', {})`` — raises ``AttributeError`` at request time,
not at import. Anywhere the test itself does not run, the breakage is
invisible: that is how 33 integration/conformance tests sat red behind a
default-skipped marker and an ungated CI tier. This scan turns that runtime
failure into a collection-time one, repo-wide, with no allowlist.

**What this guard does not catch.** Two known gaps, both deliberate:

- *Indirect* mapping use — passing the ``Connection`` to a helper that
  subscripts it (``parse_cookies(conn)`` was one of the 33) — needs call-graph
  analysis this scan does not do.
- Naming the parameter ``scope``. The word ``scope`` in this codebase means a
  genuine ASGI scope dict, so the name misdescribes a ``Connection`` and is
  what invites the mapping misuse. It is not asserted here only because ~200
  handlers in the existing corpus carry the name while behaving correctly;
  the rename is tracked as a follow-up, not a rule that has been waived.
"""
import ast
import pathlib

import pytest


_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
_CORPUS_DIRS = ('tests', 'examples')

# Registration decorators: ``@app.route(...)``, ``@group.route(...)``,
# ``@app.websocket(...)``. Matched on the attribute name alone — the receiver
# is a local variable whose type the scan cannot know.
_REGISTRATION_ATTRS = frozenset({'route', 'route_fn', 'websocket'})

# Mapping operations a ``Connection`` does not answer. ``conn.state.get`` and
# ``conn.headers.get`` are attribute chains, not calls on the parameter itself,
# so they are not matched.
_MAPPING_METHODS = frozenset({
    'get', 'setdefault', 'keys', 'items', 'values', 'pop', 'update',
})

# Handlers that genuinely receive an ASGI scope dict, keyed by
# ``<path-relative-to-repo>::<function name>``. A registered handler only ever
# sees a scope dict on the ``BB_FORCE_ASGI_SCOPE`` compat lane; anything listed
# here must be exercising that lane deliberately.
_ASGI_LANE_ALLOWLIST: frozenset[str] = frozenset()


def _iter_corpus_files():
    for d in _CORPUS_DIRS:
        root = _REPO_ROOT / d
        if not root.is_dir():
            continue
        for path in root.rglob('*.py'):
            if '__pycache__' in path.parts:
                continue
            yield path


def _is_registration_decorator(node: ast.expr) -> bool:
    """True for ``@x.route(...)`` / ``@x.websocket(...)`` and their bare forms."""
    if isinstance(node, ast.Call):
        node = node.func
    return isinstance(node, ast.Attribute) and node.attr in _REGISTRATION_ATTRS


def _registered_middleware_names(tree: ast.AST) -> set[str]:
    """Names passed to ``app.use(mw)`` or ``middlewares=[mw, ...]`` in *tree*.

    Only bare ``Name`` references are collected — a ``TrustedProxy([...])``
    instance is a class, not a def this scan can inspect.
    """
    names: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if isinstance(node.func, ast.Attribute) and node.func.attr == 'use':
            names.update(a.id for a in node.args if isinstance(a, ast.Name))
        for kw in node.keywords:
            if kw.arg == 'middlewares' and isinstance(kw.value, (ast.List, ast.Tuple)):
                names.update(e.id for e in kw.value.elts if isinstance(e, ast.Name))
    return names


def _handler_defs(tree: ast.AST):
    """Yield every function in *tree* registered as a handler or middleware."""
    middleware_names = _registered_middleware_names(tree)
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if (any(_is_registration_decorator(d) for d in node.decorator_list)
                or node.name in middleware_names):
            yield node


def _param_names(fn) -> list[str]:
    a = fn.args
    return [p.arg for p in (*a.posonlyargs, *a.args, *a.kwonlyargs)]


def _mapping_uses(fn, param: str) -> bool:
    """True if *param* is subscripted or has a mapping method called on it."""
    for node in ast.walk(fn):
        if isinstance(node, ast.Subscript):
            if isinstance(node.value, ast.Name) and node.value.id == param:
                return True
        elif isinstance(node, ast.Call):
            f = node.func
            if (isinstance(f, ast.Attribute) and f.attr in _MAPPING_METHODS
                    and isinstance(f.value, ast.Name) and f.value.id == param):
                return True
    return False


def _scan() -> list[str]:
    """Return the labels of handlers using their Connection as a mapping."""
    offenders: list[str] = []
    for path in _iter_corpus_files():
        try:
            tree = ast.parse(path.read_text(encoding='utf-8'))
        except SyntaxError:
            continue  # deliberately-malformed fixture source
        rel = path.relative_to(_REPO_ROOT).as_posix()
        for fn in _handler_defs(tree):
            label = f'{rel}::{fn.name}'
            if label in _ASGI_LANE_ALLOWLIST:
                continue
            params = _param_names(fn)
            if params and _mapping_uses(fn, params[0]):
                offenders.append(label)
    return offenders


def test_no_registered_handler_treats_its_connection_as_a_mapping():
    offenders = _scan()
    assert not offenders, (
        'A Connection is not a mapping — conn["headers"] / conn.get(...) / '
        f'conn.setdefault(...) raise AttributeError at request time: {sorted(offenders)}. '
        'Use the typed attributes instead (conn.headers, conn.query_string, '
        'conn.cookies, conn.state, conn.extensions). If the handler genuinely '
        'runs on the BB_FORCE_ASGI_SCOPE compat lane, add it to '
        '_ASGI_LANE_ALLOWLIST with a note.')


def test_allowlist_entries_exist():
    """Guard against the allowlist rotting: every entry must name a real def."""
    for label in _ASGI_LANE_ALLOWLIST:
        rel, _, fn_name = label.partition('::')
        path = _REPO_ROOT / rel
        assert path.exists(), f'_ASGI_LANE_ALLOWLIST names a missing file: {rel}'
        tree = ast.parse(path.read_text(encoding='utf-8'))
        assert any(
            isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == fn_name
            for n in ast.walk(tree)
        ), f'_ASGI_LANE_ALLOWLIST names a missing function: {label}'


def test_scan_detects_the_offending_shape():
    """The guard must fail on the shape it exists to forbid.

    Without this, an accidental change to the AST walk (a renamed decorator, a
    dropped branch) would silently turn the guard into a no-op that passes on
    an empty offender list. The source below is the real pre-fix shape from
    ``tests/integration/test_middleware_composition.py``.
    """
    source = '''
async def mw(scope, receive, send, call_next):
    scope.setdefault('state', {})['x'] = 1
    await call_next(scope, receive, send)

@app.route(path='/x', middlewares=[mw])
async def handler(scope):
    return {'q': scope.get('query_string', b'')}

@app.websocket(path='/ws')
async def ws(conn, receive, send):
    return conn['subprotocols']
'''
    tree = ast.parse(source)
    found = {fn.name for fn in _handler_defs(tree)}
    assert found == {'mw', 'handler', 'ws'}, (
        f'the scan no longer recognises registered handlers: found {found}')
    for fn in _handler_defs(tree):
        params = _param_names(fn)
        assert _mapping_uses(fn, params[0]), f'{fn.name} should have been flagged'


@pytest.mark.parametrize('source, why', [
    ("@app.route(path='/x')\nasync def h(conn):\n    return conn.headers.get(b'a')",
     'conn.headers.get is an attribute chain, not a call on conn'),
    ("@app.route(path='/x')\nasync def h(conn):\n    conn.state['k'] = 1",
     'subscripting conn.state is fine; only conn itself is forbidden'),
    ("@app.route(path='/x')\nasync def h(conn):\n    return conn.cookies",
     'plain attribute access is the sanctioned form'),
    ("async def plain(scope, receive, send):\n    return scope['type']",
     'an unregistered ASGI app is not a BlackBull handler'),
])
def test_scan_does_not_flag_legitimate_shapes(source, why):
    tree = ast.parse(source)
    handlers = list(_handler_defs(tree))
    for fn in handlers:
        assert not _mapping_uses(fn, _param_names(fn)[0]), why
