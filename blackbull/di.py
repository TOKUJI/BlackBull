"""Per-request dependency injection for simplified handlers.

``Depends`` marks a simplified-handler parameter as *provided by the
framework* rather than by the request::

    async def get_db():                    # async-generator provider
        pool = await create_pool()
        try:
            yield pool                     # ← injected value
        finally:
            await pool.close()             # ← runs after the response is sent

    @app.route(path='/items/{id:int}')
    async def get_item(id: int, db=Depends(get_db)):
        return await db.fetch_item(id)

Design: everything is resolved at **registration time** on the
``_adapt_handler`` seam — a handler that declares no ``Depends`` parameter
compiles to exactly the wrapper it compiled to before this module existed
(no per-request stack, no empty dependency loop).  Contrast FastAPI, which
enters two ``AsyncExitStack``s and runs ``solve_dependencies()`` on every
request even with an empty dependency list.
"""
import ast
import inspect
import textwrap
import warnings
from contextlib import asynccontextmanager
from typing import Any, Callable

__all__ = ['Depends']


def _cleanup_after_bare_yield(provider) -> bool:
    """True when *provider* has cleanup code that an exception would skip.

    An async-generator provider is driven through
    :func:`~contextlib.asynccontextmanager`, so an exception in the handler is
    re-raised **at the yield**.  Statements written after a bare ``yield``
    therefore never run on that path — the resource leaks precisely when
    something went wrong.  A WebSocket makes this bite harder than HTTP,
    because a socket ends by exception far more often than a request does.

    The shape reported is narrow on purpose: a ``yield`` that is not inside a
    ``try`` at all, with at least one statement reachable after it — reachable,
    not merely later in the file, so an early-exit ``yield``/``return`` branch
    is not charged for the wrapped yield further down.  A yield inside any
    ``try`` is left alone — ``finally`` covers every path, and ``except`` /
    ``else`` mean the author is deliberately telling success from failure (the
    commit-or-rollback provider).  A yield with nothing after it has no
    cleanup to lose, and an ``async with`` around the yield cleans up by
    itself.  Returns False rather than guessing when the source is
    unavailable (C-defined, REPL, ``exec``) — a diagnostic must never be the
    reason registration fails.
    """
    try:
        tree = ast.parse(textwrap.dedent(inspect.getsource(provider)))
    except (OSError, TypeError, SyntaxError, IndentationError):
        return False

    fn = next((n for n in ast.walk(tree)
               if isinstance(n, ast.AsyncFunctionDef)), None)
    if fn is None:
        return False

    guarded = {id(y) for t in ast.walk(fn) if isinstance(t, ast.Try)
               for stmt in t.body for y in ast.walk(stmt)
               if isinstance(y, (ast.Yield, ast.YieldFrom))}

    parent = {id(c): n for n in ast.walk(fn) for c in ast.iter_child_nodes(n)}
    block = {id(st): (lst, i, n)
             for n in ast.walk(fn)
             for _, lst in ast.iter_fields(n) if isinstance(lst, list)
             for i, st in enumerate(lst) if isinstance(st, ast.stmt)}

    for y in (n for n in ast.walk(fn) if isinstance(n, (ast.Yield, ast.YieldFrom))):
        if id(y) in guarded:
            continue
        if _reaches_a_statement_after(y, fn, parent, block):
            return True
    return False


def _reaches_a_statement_after(y, fn, parent, block) -> bool:
    """True when a statement can run after *y* resumes.

    Asked per enclosing block rather than per line: a degraded-mode provider
    exits early (``yield None`` then ``return``) and the resource-holding
    yield sits further down, wrapped.  Counting every later *line* in the
    function reports that shape as a leak, when the statements counted are on
    a path the yield never reaches.
    """
    node = y
    while id(node) in parent and not isinstance(node, ast.stmt):
        node = parent[id(node)]

    while True:
        entry = block.get(id(node))
        if entry is None:
            return False
        siblings, index, owner = entry
        rest = siblings[index + 1:]
        if rest:
            # ``return``/``raise`` right after the yield ends the path without
            # running anything; anything else is the cleanup being warned about.
            return not isinstance(rest[0], (ast.Return, ast.Raise))
        if owner is fn or not isinstance(owner, ast.stmt):
            return False
        node = owner


class Depends:
    """Declare a per-request provider for one simplified-handler parameter.

    Use as the parameter's *default value*: ``db=Depends(get_db)``.

    Provider forms (detected once, here):

    * **async generator** — yields the injected value exactly once; the code
      after ``yield`` (or the ``finally`` block) runs after the response has
      been sent, LIFO when several providers are active.
    * **async function** — awaited for the value; no cleanup.
    * **sync function** — called for the value; no cleanup.

    Args:
        provider: Zero-parameter callable in one of the three forms above.
        use_cache: When ``True`` (default), parameters of one handler that
            name the *same* provider share a single instance per request;
            ``use_cache=False`` calls the provider once per parameter.

    Raises:
        TypeError: At construction, when *provider* is not callable, takes
            parameters (including a nested ``Depends`` default — not
            supported in v1), or is a sync generator function.
    """

    __slots__ = ('provider', 'use_cache', '_kind', '_acm_factory')

    _ASYNC_GEN = 'async_gen'
    _ASYNC_FN = 'async_fn'
    _SYNC_FN = 'sync_fn'

    def __init__(self, provider: Callable[[], Any], *, use_cache: bool = True):
        if not callable(provider):
            raise TypeError(
                f'Depends(provider): provider must be callable, got '
                f'{type(provider).__name__!r}')

        try:
            sig = inspect.signature(provider)
        except (TypeError, ValueError):
            sig = None
        if sig is not None and sig.parameters:
            if any(isinstance(p.default, Depends) for p in sig.parameters.values()):
                raise TypeError(
                    f'Depends provider {getattr(provider, "__name__", provider)!r} '
                    f'declares a nested Depends parameter. Nested dependencies '
                    f'are not supported in v1 — compose inside the provider '
                    f'body instead (call or close over the other provider).')
            raise TypeError(
                f'Depends provider {getattr(provider, "__name__", provider)!r} '
                f'must take no parameters; it declares '
                f'{sorted(sig.parameters)!r}.')

        self.provider = provider
        self.use_cache = use_cache
        self._acm_factory = None
        if inspect.isasyncgenfunction(provider):
            self._kind = self._ASYNC_GEN
            # asynccontextmanager gives the exact lifecycle wanted: one yield
            # (a second yield raises RuntimeError), exceptions thrown into
            # the generator at the yield point, cleanup via AsyncExitStack.
            self._acm_factory = asynccontextmanager(provider)
            # Registration-time signal, not a runtime one: cleanup after a bare
            # yield is skipped on exactly the paths that need it most.  Warn
            # rather than raise — the shape is legal, and a provider whose
            # handler never fails works fine today.
            if _cleanup_after_bare_yield(provider):
                warnings.warn(
                    f'Depends provider '
                    f'{getattr(provider, "__name__", provider)!r} has cleanup '
                    f'after a bare `yield`. It is skipped when the handler '
                    f'raises — and on a WebSocket, when the peer disconnects. '
                    f'Wrap the yield so cleanup always runs:\n'
                    f'    try:\n'
                    f'        yield value\n'
                    f'    finally:\n'
                    f'        ...  # release here\n'
                    f'Use `except`/`else` instead if the cleanup needs to tell '
                    f'success from failure (commit vs rollback).',
                    UserWarning, stacklevel=2)
        elif inspect.iscoroutinefunction(provider):
            self._kind = self._ASYNC_FN
        elif inspect.isgeneratorfunction(provider):
            raise TypeError(
                f'Depends provider {getattr(provider, "__name__", provider)!r} '
                f'is a sync generator; use an async generator '
                f'(``async def`` + ``yield``) for value-plus-cleanup providers.')
        else:
            self._kind = self._SYNC_FN

    def __repr__(self) -> str:
        name = getattr(self.provider, '__name__', repr(self.provider))
        cache = '' if self.use_cache else ', use_cache=False'
        return f'Depends({name}{cache})'


async def _resolve_depends(dep: Depends, stack, cache: dict) -> Any:
    """Resolve one ``Depends`` parameter inside the per-request *stack*.

    *cache* maps provider → value for ``use_cache=True`` sharing within a
    single request; it is created per request by the handler wrapper.
    """
    provider = dep.provider
    if dep.use_cache and provider in cache:
        return cache[provider]
    if dep._kind == Depends._ASYNC_GEN:
        value = await stack.enter_async_context(dep._acm_factory())
    elif dep._kind == Depends._ASYNC_FN:
        value = await provider()
    else:
        value = provider()
    if dep.use_cache:
        cache[provider] = value
    return value
