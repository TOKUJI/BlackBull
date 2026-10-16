"""Tests reach BlackBull through public seams; private reach is a shrinking allowance.

Counted per test file: ``x._name`` on anything but ``self``/``cls``; ``self._name``
inside a test class that subclasses a BlackBull class; a private name given as a
string to ``getattr``/``setattr``/``hasattr``/``delattr``, ``monkeypatch.setattr``
or ``patch.object``, or indexed out of ``vars(x)``/``x.__dict__``; a ``patch``
target with a private segment; and imports of private names or modules from
``blackbull``.  A file may not exceed its allowance in
``private_access_allowlist.json``, and an allowance that is no longer used must
be lowered, so the list only shrinks.
"""
import ast
import json
import pathlib

TESTS = pathlib.Path(__file__).resolve().parents[1]
ALLOWLIST = pathlib.Path(__file__).with_name('private_access_allowlist.json')
_BY_NAME = {'getattr', 'setattr', 'hasattr', 'delattr'}
_BY_STRING = {'setattr', 'delattr', 'object'}       # monkeypatch.setattr, patch.object


def _private(name: str) -> bool:
    return name.startswith('_') and not name.startswith('__')


def _private_string(node) -> bool:
    return isinstance(node, ast.Constant) and isinstance(node.value, str) and (
        _private(node.value)
        or (node.value.startswith('blackbull.') and any(map(_private, node.value.split('.')))))


def _blackbull_names(tree) -> set[str]:
    """Names bound to BlackBull objects or modules, then local classes deriving from them."""
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and (node.module or '').startswith('blackbull'):
            names.update(a.asname or a.name for a in node.names)
        elif isinstance(node, ast.Import):
            names.update(a.asname or a.name.split('.')[0]
                         for a in node.names if a.name.startswith('blackbull'))
    classes = [n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)]
    while grown := {c.name for c in classes if c.name not in names and _subclasses(c, names)}:
        names |= grown
    return names


def _subclasses(cls: ast.ClassDef, names: set[str]) -> bool:
    def root(b):
        while isinstance(b, ast.Attribute):
            b = b.value
        return b.id if isinstance(b, ast.Name) else None
    return any(root(b) in names for b in cls.bases)


def private_references(source: str) -> int:
    tree = ast.parse(source)
    names = _blackbull_names(tree)
    inherited = {id(n) for cls in ast.walk(tree)
                 if isinstance(cls, ast.ClassDef) and _subclasses(cls, names)
                 for n in ast.walk(cls)}
    count = 0
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and _private(node.attr):
            on_self = isinstance(node.value, ast.Name) and node.value.id in ('self', 'cls')
            count += not on_self or id(node) in inherited
        elif isinstance(node, ast.Call):
            f = node.func
            if (isinstance(f, ast.Name) and f.id in _BY_NAME) or (
                    isinstance(f, ast.Attribute) and f.attr in _BY_STRING):
                count += any(map(_private_string, [*node.args[:2], *(
                    k.value for k in node.keywords if k.arg in ('attribute', 'name'))]))
            elif (isinstance(f, ast.Name) and f.id == 'patch') or (
                    isinstance(f, ast.Attribute) and f.attr == 'patch'):
                count += any(map(_private_string, node.args[:1]))
        elif isinstance(node, ast.Subscript) and _private_string(node.slice):
            v = node.value
            count += ((isinstance(v, ast.Call) and isinstance(v.func, ast.Name) and v.func.id == 'vars')
                      or (isinstance(v, ast.Attribute) and v.attr == '__dict__'))
        elif isinstance(node, ast.ImportFrom) and (node.module or '').startswith('blackbull'):
            count += any(_private(part) for part in node.module.split('.'))
            count += sum(_private(alias.name) for alias in node.names)
        elif isinstance(node, ast.Import):
            count += sum(any(_private(p) for p in a.name.split('.'))
                         for a in node.names if a.name.startswith('blackbull'))
    return count


def test_every_reach_form_is_counted():
    probe = '''
from blackbull.server.http1_actor import HTTP1Actor
actor._w; getattr(actor, "_w"); setattr(actor, "_w", 1); vars(actor)["_w"]; actor.__dict__["_w"]
monkeypatch.setattr(actor, "_w", 1); patch.object(HTTP1Actor, "_w"); patch("blackbull.server._x")
class Fake(HTTP1Actor):
    def f(self): return self._w
class Deeper(Fake):
    def f(self): return self._w
class Own:
    def f(self): return self._w
patch.object(HTTP1Actor, attribute="_w")
import blackbull.server.http1_actor as h
class ViaModule(h.HTTP1Actor):
    def f(self): return self._w
'''
    assert private_references(probe) == 12


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
