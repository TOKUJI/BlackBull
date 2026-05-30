"""Integration tests for middleware composition — per-route, groups, short-circuit, global."""
import asyncio
from multiprocessing import Process

import httpx
import pytest

from blackbull import BlackBull, JSONResponse
from .conftest import live_server


def _make_app() -> BlackBull:
    app = BlackBull()

    # --- per-route middleware ---

    async def add_header_mw(scope, receive, send, call_next):
        scope.setdefault('state', {})['mw_ran'] = True
        await call_next(scope, receive, send)

    @app.route(path='/with-mw', middlewares=[add_header_mw])
    async def with_mw(scope):
        return {'mw_ran': scope.get('state', {}).get('mw_ran', False)}

    @app.route(path='/without-mw')
    async def without_mw(scope):
        return {'mw_ran': scope.get('state', {}).get('mw_ran', False)}

    # --- middleware order: each appends a tag; handler returns the list ---

    async def outer_mw(scope, receive, send, call_next):
        scope.setdefault('state', {}).setdefault('order', []).append('outer')
        await call_next(scope, receive, send)

    async def inner_mw(scope, receive, send, call_next):
        scope.setdefault('state', {}).setdefault('order', []).append('inner')
        await call_next(scope, receive, send)

    @app.route(path='/order', middlewares=[outer_mw, inner_mw])
    async def order_handler(scope):
        scope['state']['order'].append('handler')
        return scope['state']['order']

    # --- short-circuit: handler must not be called ---

    _handler_called = []

    async def blocking_mw(scope, receive, send, call_next):
        await send({'type': 'http.response.start', 'status': 403, 'headers': []})
        await send({'type': 'http.response.body', 'body': b'blocked', 'more_body': False})
        # deliberately does NOT call call_next

    @app.route(path='/blocked', middlewares=[blocking_mw])
    async def blocked_handler():
        _handler_called.append(True)
        return {}

    # --- route group ---

    async def group_mw(scope, receive, send, call_next):
        scope.setdefault('state', {})['group'] = True
        await call_next(scope, receive, send)

    group = app.group(middlewares=[group_mw])

    @group.route(path='/group/resource')
    async def group_resource(scope):
        return {'group': scope.get('state', {}).get('group', False)}

    # --- global middleware ---

    async def global_mw(scope, receive, send, call_next):
        scope.setdefault('state', {})['global'] = True
        await call_next(scope, receive, send)

    app.use(global_mw)

    @app.route(path='/global')
    async def global_route(scope):
        return {'global': scope.get('state', {}).get('global', False)}

    return app


@pytest.fixture(scope="module")
def live():
    app = _make_app()
    with live_server(app) as handle:
        yield handle
def _base(app) -> str:
    return f'http://127.0.0.1:{app.port}'


@pytest.mark.integration
@pytest.mark.asyncio
async def test_per_route_middleware_runs(live):
    async with httpx.AsyncClient() as c:
        r = await c.get(f'{_base(live)}/with-mw')
    assert r.status_code == 200
    assert r.json()['mw_ran'] is True


@pytest.mark.integration
@pytest.mark.asyncio
async def test_per_route_middleware_does_not_affect_other_routes(live):
    async with httpx.AsyncClient() as c:
        r = await c.get(f'{_base(live)}/without-mw')
    assert r.status_code == 200
    assert r.json()['mw_ran'] is False


@pytest.mark.integration
@pytest.mark.asyncio
async def test_per_route_middleware_order(live):
    """Middleware runs outer-before-inner; handler sees both; list return works."""
    async with httpx.AsyncClient() as c:
        r = await c.get(f'{_base(live)}/order')
    assert r.status_code == 200
    assert r.json() == ['outer', 'inner', 'handler']


@pytest.mark.integration
@pytest.mark.asyncio
async def test_short_circuit_skips_handler(live):
    async with httpx.AsyncClient() as c:
        r = await c.get(f'{_base(live)}/blocked')
    assert r.status_code == 403
    assert r.content == b'blocked'


@pytest.mark.integration
@pytest.mark.asyncio
async def test_route_group_middleware_runs(live):
    async with httpx.AsyncClient() as c:
        r = await c.get(f'{_base(live)}/group/resource')
    assert r.status_code == 200
    assert r.json()['group'] is True


@pytest.mark.integration
@pytest.mark.asyncio
async def test_global_middleware_affects_all_routes(live):
    async with httpx.AsyncClient() as c:
        r1 = await c.get(f'{_base(live)}/global')
        r2 = await c.get(f'{_base(live)}/without-mw')
    assert r1.json()['global'] is True
    assert r2.json()['mw_ran'] is False   # per-route mw absent, but global ran
