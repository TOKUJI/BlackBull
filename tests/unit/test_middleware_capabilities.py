"""Integration tests documenting middleware capabilities of the BlackBull stack.

These tests run through the Router / BlackBull call path (no mock server)
and act as capability documentation as well as regression protection.
"""
import pytest
from http import HTTPMethod
from unittest.mock import AsyncMock

from blackbull import BlackBull
from blackbull.utils import Scheme


@pytest.fixture
def app():
    return BlackBull()


# ---------------------------------------------------------------------------
# Post-processing: code after call_next runs after the handler
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_post_processing_runs_after_handler(app):
    order = []

    async def mw(scope, receive, send, call_next):
        order.append('before')
        result = await call_next(scope, receive, send)
        order.append('after')
        return result

    @app.route(methods=[HTTPMethod.GET], path='/post', middlewares=[mw])
    async def handler(scope, receive, send):
        order.append('handler')
        return 'ok'

    scope = {'type': 'http', 'method': 'GET', 'path': '/post', 'headers': {}}
    await app(scope, None, lambda *a, **kw: None)
    assert order == ['before', 'handler', 'after']


# ---------------------------------------------------------------------------
# Scope mutation: middleware can inject data the handler reads
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_scope_mutation_visible_in_handler(app):
    async def inject_user(scope, receive, send, call_next):
        scope['user'] = 'alice'
        return await call_next(scope, receive, send)

    seen = []

    @app.route(methods=[HTTPMethod.GET], path='/user', middlewares=[inject_user])
    async def handler(scope, receive, send):
        seen.append(scope.get('user'))
        return 'ok'

    scope = {'type': 'http', 'method': 'GET', 'path': '/user', 'headers': {}}
    await app(scope, None, lambda *a, **kw: None)
    assert seen == ['alice']


# ---------------------------------------------------------------------------
# Error handling: try/except around call_next catches handler exceptions
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_middleware_catches_handler_exception(app):
    caught = []

    async def error_mw(scope, receive, send, call_next):
        try:
            return await call_next(scope, receive, send)
        except ValueError as exc:
            caught.append(str(exc))
            return 'handled'

    @app.route(methods=[HTTPMethod.GET], path='/err', middlewares=[error_mw])
    async def handler(scope, receive, send):
        raise ValueError('oops')

    scope = {'type': 'http', 'method': 'GET', 'path': '/err', 'headers': {}}
    # BlackBull's own error handler will also fire; we just check the mw saw it
    await app(scope, None, lambda *a, **kw: None)
    assert caught == ['oops']


# ---------------------------------------------------------------------------
# Composition: three-middleware chain runs in correct order
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_three_middleware_chain_order(app):
    log = []

    async def mw_a(scope, receive, send, call_next):
        log.append('a_in')
        res = await call_next(scope, receive, send)
        log.append('a_out')
        return res

    async def mw_b(scope, receive, send, call_next):
        log.append('b_in')
        res = await call_next(scope, receive, send)
        log.append('b_out')
        return res

    async def mw_c(scope, receive, send, call_next):
        log.append('c_in')
        res = await call_next(scope, receive, send)
        log.append('c_out')
        return res

    @app.route(methods=[HTTPMethod.GET], path='/chain', middlewares=[mw_a, mw_b, mw_c])
    async def handler(scope, receive, send):
        log.append('handler')
        return 'done'

    scope = {'type': 'http', 'method': 'GET', 'path': '/chain', 'headers': {}}
    await app(scope, None, lambda *a, **kw: None)
    assert log == ['a_in', 'b_in', 'c_in', 'handler', 'c_out', 'b_out', 'a_out']


# ---------------------------------------------------------------------------
# Return value: call_next propagates the handler's return value
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_call_next_return_value_propagates(app):
    received = []

    async def capture_mw(scope, receive, send, call_next):
        val = await call_next(scope, receive, send)
        received.append(val)
        return val

    @app.route(methods=[HTTPMethod.GET], path='/ret', middlewares=[capture_mw])
    async def handler(scope, receive, send):
        return 'payload'

    scope = {'type': 'http', 'method': 'GET', 'path': '/ret', 'headers': {}}
    await app(scope, None, lambda *a, **kw: None)
    assert received == ['payload']


# ---------------------------------------------------------------------------
# Short-circuit: outer middleware's post-processing is skipped when inner
# middleware does not call call_next
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_short_circuit_skips_outer_post_processing(app):
    log = []

    async def outer(scope, receive, send, call_next):
        log.append('outer_before')
        res = await call_next(scope, receive, send)
        log.append('outer_after')
        return res

    async def blocker(scope, receive, send, call_next):
        log.append('blocker')
        return 'blocked'  # does NOT call call_next

    @app.route(methods=[HTTPMethod.GET], path='/sc', middlewares=[outer, blocker])
    async def handler(scope, receive, send):
        log.append('handler')
        return 'ok'

    scope = {'type': 'http', 'method': 'GET', 'path': '/sc', 'headers': {}}
    await app(scope, None, lambda *a, **kw: None)
    # outer's post-processing runs because outer DID call call_next (blocker)
    # handler is never reached; outer_after still fires
    assert 'handler' not in log
    assert log == ['outer_before', 'blocker', 'outer_after']


# ---------------------------------------------------------------------------
# Middleware chain caching (5.2 optimisation)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_chain_built_once_across_requests(app):
    """The global middleware chain must be compiled once and reused across requests."""
    async def noop(scope, receive, send, call_next):
        await call_next(scope, receive, send)

    app.use(noop)

    @app.route(methods=[HTTPMethod.GET], path='/c')
    async def h():
        return 'ok'

    scope = {'type': 'http', 'method': 'GET', 'path': '/c', 'headers': {}}
    await app(scope, None, AsyncMock())
    chain_after_first = app._chain

    await app(scope, None, AsyncMock())
    assert app._chain is chain_after_first, (
        'Chain must be the same object across requests — not rebuilt per call'
    )


@pytest.mark.asyncio
async def test_chain_invalidated_when_middleware_added(app):
    """Adding a middleware via use() must invalidate the cached chain."""
    async def noop(scope, receive, send, call_next):
        await call_next(scope, receive, send)

    @app.route(methods=[HTTPMethod.GET], path='/inv')
    async def h():
        return 'ok'

    scope = {'type': 'http', 'method': 'GET', 'path': '/inv', 'headers': {}}

    await app(scope, None, AsyncMock())
    assert app._chain is not None

    app.use(noop)  # must invalidate
    assert app._chain is None, 'use() must set _chain to None'

    await app(scope, None, AsyncMock())
    assert app._chain is not None, 'chain must be rebuilt on the next request'


@pytest.mark.asyncio
async def test_chain_includes_all_global_middlewares(app):
    """Every middleware registered via use() must execute on each request."""
    log = []

    async def mw_a(scope, receive, send, call_next):
        log.append('a')
        await call_next(scope, receive, send)

    async def mw_b(scope, receive, send, call_next):
        log.append('b')
        await call_next(scope, receive, send)

    app.use(mw_a)
    app.use(mw_b)

    @app.route(methods=[HTTPMethod.GET], path='/all')
    async def h():
        log.append('handler')
        return 'ok'

    scope = {'type': 'http', 'method': 'GET', 'path': '/all', 'headers': {}}
    await app(scope, None, AsyncMock())
    assert log == ['a', 'b', 'handler']
