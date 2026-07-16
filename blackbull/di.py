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

Design (Sprint 74): everything is resolved at **registration time** on the
``_adapt_handler`` seam — a handler that declares no ``Depends`` parameter
compiles to exactly the wrapper it compiled to before this module existed
(no per-request stack, no empty dependency loop).  Contrast FastAPI, which
enters two ``AsyncExitStack``s and runs ``solve_dependencies()`` on every
request even with an empty dependency list.

v1 scope fences (add on real demand, not speculatively):

* providers take **no parameters** — a provider that itself declares
  ``Depends`` (nesting) is a registration-time ``TypeError``;
* simplified handlers only — middleware and full ``(scope, receive, send)``
  handlers are unchanged;
* no interface binding and no interception: duck typing and the event API
  (``@app.intercept``) already cover those.
"""
import inspect
from contextlib import asynccontextmanager
from typing import Any, Callable

__all__ = ['Depends']


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
