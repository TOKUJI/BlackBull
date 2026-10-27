"""The zero-overhead pin for ``_adapt_handler``.

The anti-FastAPI design point: parameters and ``Depends`` are resolved at
registration time.  A handler without ``Depends`` compiles to the one plain
wrapper, with no dependency machinery, and one without parameters binds
nothing at all.  FastAPI, by contrast, runs ``solve_dependencies()`` + two
``AsyncExitStack``s per request even with no dependencies declared.
"""
from dataclasses import dataclass
from blackbull import Depends, Connection as Request  # deprecated alias
from blackbull.router import _adapt_handler


@dataclass
class _Item:
    name: str


async def _plain(): return 'x'
async def _path_only(item_id: int): return item_id
async def _full_legacy(item_id: int, body: bytes, conn, request: Request): pass
async def _dataclass_body(item: _Item): pass
async def _with_query(q: str, page: int = 1): pass
def _provider(): return 'db'
async def _with_depends(db=Depends(_provider)): pass


def test_handlers_without_depends_share_one_wrapper_code_object():
    # Every closure the plain path emits shares the same code object —
    # the strongest form of "byte-identical adapted functions".
    plain = [
        _adapt_handler(_plain, '/'),
        _adapt_handler(_path_only, '/items/{item_id}'),
        _adapt_handler(_full_legacy, '/items/{item_id}'),
        _adapt_handler(_dataclass_body, '/items'),
        _adapt_handler(_with_query, '/search'),
    ]
    assert len({w.__code__ for w in plain}) == 1


def test_a_depends_handler_uses_a_different_wrapper():
    assert _adapt_handler(_with_depends, '/').__code__ is not _adapt_handler(_plain, '/').__code__


def test_the_plain_wrapper_never_references_dependency_machinery():
    plain = _adapt_handler(_full_legacy, '/items/{item_id}')
    names = set(plain.__code__.co_names) | set(plain.__code__.co_freevars)
    assert 'AsyncExitStack' not in names
    assert not [n for n in names if 'depend' in n.lower() or 'resolve' in n.lower()]


def test_a_handler_without_parameters_binds_nothing():
    plain = _adapt_handler(_plain, '/')
    cells = dict(zip(plain.__code__.co_freevars, plain.__closure__))
    assert cells['bind'].cell_contents is None
