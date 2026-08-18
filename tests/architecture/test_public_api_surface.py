"""``blackbull.__all__`` is the package's public surface, and it must stay true.

The package ships ``py.typed``.  For an inline-typed package the typing spec
treats a name imported as ``from .x import Y`` as *private* unless it is
re-exported — either written ``from .x import Y as Y`` or listed in
``__all__``.  So ``__all__`` is not decoration here; it is the statement that
makes these names part of the typed contract.

It also protects against the failure that prompted it.  A bare ``import x`` at
the top of ``__init__.py`` silently publishes ``blackbull.x``: that is how
``blackbull.logging`` came to resolve to the *standard library* ``logging``
module, sitting next to the real ``blackbull.logger``, and how
``blackbull.PackageNotFoundError`` came to be ``importlib.metadata``'s.  Both
are now imported under private aliases, and the tests below fail if a third
one appears.

``__all__`` only governs ``import *`` at runtime, so it cannot by itself stop
``from blackbull import <leak>``.  The private-alias convention is what does
that; ``__all__`` records the intent and these tests keep the two in step.
"""
from __future__ import annotations

import warnings

import blackbull


def _public_attrs() -> set[str]:
    return {n for n in dir(blackbull) if not n.startswith('_')}


def _non_module_public() -> set[str]:
    return {n for n in _public_attrs()
            if type(getattr(blackbull, n)).__name__ != 'module'}


def test_all_is_defined():
    assert hasattr(blackbull, '__all__'), (
        'blackbull defines no __all__.  The package ships py.typed, so '
        'without it the names it re-exports are not part of the typed '
        'contract for a strict type checker.')
    assert isinstance(blackbull.__all__, (list, tuple))


def test_every_exported_name_exists():
    missing = [n for n in blackbull.__all__ if not hasattr(blackbull, n)]
    assert not missing, (
        f'__all__ promises names the package does not have: {missing}.  '
        f'`from blackbull import *` would raise AttributeError.')


def test_no_public_name_is_missing_from_all():
    """A new public name must be a decision, not a side effect.

    Everything the package exposes that is not a submodule should be in
    ``__all__``.  When this fails the usual cause is a bare ``import x`` added
    to ``__init__.py``, which publishes ``blackbull.x`` without anyone
    intending it — import it as ``import x as _x`` instead.
    """
    undeclared = _non_module_public() - set(blackbull.__all__)
    assert not undeclared, (
        f'public names not in __all__: {sorted(undeclared)}.  Either add them '
        f'deliberately, or — if one is an import that leaked — bind it '
        f'privately (`import x as _x`).')


def test_nothing_from_outside_the_package_is_exported():
    """The leak this file exists for.

    A name whose ``__module__`` is not under ``blackbull`` is something we
    imported for our own use and published by accident.  Type aliases are the
    legitimate exception: they are built from ``typing`` / ``collections.abc``
    and carry those modules' names.
    """
    allowed_foreign = {
        # ASGI callable/event aliases — genuinely ours, but a typing alias
        # reports the module it was constructed from.
        'ASGIReceiveCallable', 'ASGIReceiveEvent',
        'ASGISendCallable', 'ASGISendEvent', 'EventHandler',
        # QUERY is a plain str constant (RFC 10008) and has no __module__.
        'QUERY',
    }
    foreign = []
    for name in blackbull.__all__:
        obj = getattr(blackbull, name)
        origin = getattr(obj, '__module__', None) or ''
        if origin and not origin.startswith('blackbull') \
                and name not in allowed_foreign:
            foreign.append(f'{name} (from {origin})')
    assert not foreign, (
        f'__all__ exports names that come from outside the package: '
        f'{foreign}.  Import those under a private alias instead.')


def test_the_deprecated_alias_is_not_exported():
    """``Request`` resolves through ``__getattr__`` and warns when touched.

    Listing it in ``__all__`` would make ``from blackbull import *`` emit a
    DeprecationWarning for code that never asked for the alias.
    """
    assert 'Request' not in blackbull.__all__

    with warnings.catch_warnings():
        warnings.simplefilter('error', DeprecationWarning)
        ns: dict = {}
        exec('from blackbull import *', ns)   # noqa: S102 — that is the test


def test_star_import_delivers_exactly_all():
    ns: dict = {}
    exec('from blackbull import *', ns)       # noqa: S102
    delivered = {n for n in ns if not n.startswith('_')}
    assert delivered == set(blackbull.__all__)
