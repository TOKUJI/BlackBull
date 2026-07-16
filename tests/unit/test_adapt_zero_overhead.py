"""The Sprint 74 zero-overhead pin for ``_adapt_handler``.

The anti-FastAPI design point: query params and ``Depends`` are resolved at
registration time, so a handler using *neither* feature compiles to exactly
the wrapper it compiled to before Sprint 74 — the same (shared) code object,
with no reference to the DI or query machinery.  FastAPI, by contrast, runs
``solve_dependencies()`` + two ``AsyncExitStack``s per request even with no
dependencies declared.
"""
from dataclasses import dataclass

from blackbull import Depends, Request
from blackbull.router import _adapt_handler


@dataclass
class _Item:
    name: str


async def _plain(): return 'x'
async def _path_only(item_id: int): return item_id
async def _full_legacy(item_id: int, body: bytes, scope, request: Request): pass
async def _dataclass_body(item: _Item): pass
async def _with_query(q: str, page: int = 1): pass
def _provider(): return 'db'
async def _with_depends(db=Depends(_provider)): pass


def test_handlers_without_new_features_share_one_wrapper_code_object():
    # Every closure the basic path emits shares the same code object —
    # the strongest form of "byte-identical adapted functions".
    basics = [
        _adapt_handler(_plain, '/'),
        _adapt_handler(_path_only, '/items/{item_id}'),
        _adapt_handler(_full_legacy, '/items/{item_id}'),
        _adapt_handler(_dataclass_body, '/items'),
    ]
    codes = {w.__code__ for w in basics}
    assert len(codes) == 1


def test_query_and_depends_handlers_use_a_different_wrapper():
    basic = _adapt_handler(_plain, '/')
    query = _adapt_handler(_with_query, '/search')
    depends = _adapt_handler(_with_depends, '/')
    assert query.__code__ is not basic.__code__
    assert depends.__code__ is not basic.__code__


def test_basic_wrapper_never_references_di_or_query_machinery():
    basic = _adapt_handler(_full_legacy, '/items/{item_id}')
    names = set(basic.__code__.co_names) | set(basic.__code__.co_freevars)
    assert 'AsyncExitStack' not in names
    for symbol in names:
        assert 'depends' not in symbol.lower()
        assert 'query' not in symbol.lower()
