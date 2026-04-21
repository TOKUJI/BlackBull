from logging import getLogger
from http import HTTPStatus, HTTPMethod
# import asyncio
import pytest

# Test targets
from blackbull.utils import Scheme
from blackbull.router import Router, _middleware_param, has_middleware_param
from blackbull import BlackBull
from blackbull.router import RouteGroup

logger = getLogger(__name__)


@pytest.fixture
def router():
    router = Router()
    yield router


def test_router_add(router):
    path = 'test'
    method = HTTPMethod.GET
    scheme = Scheme.http
    key = (path, method, scheme)

    logger.info('Before registration.')

    @router.route(path=path, methods=method, scheme=scheme)
    def test_fn(*args, **kwargs):
        pass

    logger.info('Registered.')
    f = router[key]

    assert f == test_fn
    # assert m == method


@pytest.mark.asyncio
async def test_router_add_middleware_stack1(router):
    path = 'test'
    scheme = Scheme.http

    async def test_fn1(scope, receive, send, inner):
        logger.info('test_fn1 starts.')
        await inner(scope, receive, send)
        logger.info('test_fn1 ends.')
        return 'fn1'

    router.route(methods=[HTTPMethod.GET], path=path, functions=[test_fn1])

    logger.info('Registered.')
    fn = router[(path, HTTPMethod.GET, scheme)]
    logger.info(fn)

    res = await fn({}, None, None)
    assert res == 'fn1'


@pytest.mark.asyncio
async def test_router_add_middleware_stack2(router):
    path = 'test'
    scheme = Scheme.http

    async def test_fn1(scope, receive, send, inner):
        logger.info('test_fn1 starts.')
        res = await inner(scope, receive, send)
        logger.info('test_fn1 ends.')
        return res + 'fn1'

    async def test_fn2(scope, receive, send, inner):
        logger.info('test_fn2 starts.')
        res = await inner(scope, receive, send)
        logger.info('test_fn2 ends.')
        return res + 'fn2'

    async def test_fn3(scope, receive, send, inner):
        logger.info('test_fn3 starts.')
        await inner(scope, receive, send)
        logger.info('test_fn3 ends.')
        return 'fn3'

    router.route(methods=[HTTPMethod.GET], path=path, functions=[test_fn1, test_fn2, test_fn3])

    logger.info('Registered.')
    fn = router[(path, HTTPMethod.GET, scheme)]
    logger.info(fn)

    res = await fn({}, None, None)
    assert res == 'fn3fn2fn1'


def test_router_add_post(router):
    path = 'test'
    scheme = Scheme.http

    @router.route(path=path, methods=[HTTPMethod.POST])
    def fn(*args, **kwargs):
        pass

    f = router[(path, HTTPMethod.POST, scheme)]

    assert f == fn


def test_router_regex(router):
    path = r'^test/\d+$'
    scheme = Scheme.http

    @router.route(path=path, methods=[HTTPMethod.GET])
    def fn(*args, **kwargs):
        pass

    f = router[('test/1234', HTTPMethod.GET, scheme)]

    assert f == fn


@pytest.mark.asyncio
async def test_router_regex_with_group_name1(router):
    path = r'^test/(?P<id_>\d+)$'
    scheme = Scheme.http

    @router.route(path=path, methods=[HTTPMethod.GET])
    async def fn(scope, receive, send):
        pass

    f = router[('test/1234', HTTPMethod.GET, scheme)]
    scope = {}
    await f(scope, None, None)
    assert scope['path_params']['id_'] == '1234'


@pytest.mark.asyncio
async def test_router_regex_with_group_name2(router):
    path = r'^test/(?P<id_>\d+)$'
    scheme = Scheme.http

    @router.route(path=path, methods=[HTTPMethod.GET])
    async def fn(scope, receive, send):
        pass

    f = router[('test/1234', HTTPMethod.GET, scheme)]
    scope = {}
    await f(scope, None, None)
    assert scope['path_params']['id_'] == '1234'


@pytest.mark.asyncio
async def test_router_F_string1(router):
    path = 'test/{id_}'
    scheme = Scheme.http

    @router.route(path=path, methods=[HTTPMethod.GET])
    async def fn(scope, receive, send):
        pass

    id_ = 'a24_12-3.4~'
    f = router[(f'test/{id_}', HTTPMethod.GET, scheme)]
    scope = {}
    await f(scope, None, None)
    assert scope['path_params']['id_'] == id_


@pytest.mark.asyncio
async def test_router_F_string2(router):
    path = 'test/{id_}/{name}'
    scheme = Scheme.http

    @router.route(path=path, methods=[HTTPMethod.GET])
    async def fn(scope, receive, send):
        pass

    id_ = 'a24_12-3.4~'
    name = 'Toshio'
    f = router[(f'test/{id_}/{name}', HTTPMethod.GET, scheme)]
    scope = {}
    await f(scope, None, None)
    assert scope['path_params']['id_'] == id_
    assert scope['path_params']['name'] == name


@pytest.mark.asyncio
async def test_router_websocket(router):
    path = 'test/{id_}'
    scheme = Scheme.websocket

    @router.route(path=path, scheme=scheme)
    async def fn(scope, receive, send):
        pass

    logger.debug('Registered')
    id_ = 'a24_12-3.4~'
    f = router[(f'test/{id_}', HTTPMethod.GET, scheme)]
    scope = {}
    await f(scope, None, None)
    assert scope['path_params']['id_'] == id_


# ---------------------------------------------------------------------------
# call_next / inner detection tests
# ---------------------------------------------------------------------------

def test_call_next_detected_as_middleware_param():
    async def mw(scope, receive, send, call_next):
        pass
    assert _middleware_param(mw) == 'call_next'
    assert has_middleware_param(mw) is True


def test_inner_still_detected_as_middleware_param():
    async def mw(scope, receive, send, inner):
        pass
    assert _middleware_param(mw) == 'inner'
    assert has_middleware_param(mw) is True


def test_plain_handler_has_no_middleware_param():
    async def handler(scope, receive, send):
        pass
    assert _middleware_param(handler) is None
    assert has_middleware_param(handler) is False


@pytest.mark.asyncio
async def test_chain_built_with_call_next_name(router):
    path = 'test_cn'

    async def mw(scope, receive, send, call_next):
        res = await call_next(scope, receive, send)
        return res + '_mw'

    async def handler(scope, receive, send):
        return 'ok'

    router.route(methods=[HTTPMethod.GET], path=path, functions=[mw, handler])
    fn = router[(path, HTTPMethod.GET, Scheme.http)]
    assert await fn({}, None, None) == 'ok_mw'


@pytest.mark.asyncio
async def test_chain_built_with_inner_name(router):
    path = 'test_inner'

    async def mw(scope, receive, send, inner):
        res = await inner(scope, receive, send)
        return res + '_mw'

    async def handler(scope, receive, send):
        return 'ok'

    router.route(methods=[HTTPMethod.GET], path=path, functions=[mw, handler])
    fn = router[(path, HTTPMethod.GET, Scheme.http)]
    assert await fn({}, None, None) == 'ok_mw'


@pytest.mark.asyncio
async def test_chain_short_circuits_when_call_next_not_called(router):
    path = 'test_sc'
    called = []

    async def mw(scope, receive, send, call_next):
        called.append('mw')
        return 'short'

    async def handler(scope, receive, send):
        called.append('handler')
        return 'handler_result'

    router.route(methods=[HTTPMethod.GET], path=path, functions=[mw, handler])
    fn = router[(path, HTTPMethod.GET, Scheme.http)]
    result = await fn({}, None, None)
    assert result == 'short'
    assert 'handler' not in called


# ---------------------------------------------------------------------------
# middlewares= parameter tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_middlewares_decorator_registers_route(router):
    path = 'test_mw_dec'

    async def mw(scope, receive, send, call_next):
        return await call_next(scope, receive, send)

    @router.route(methods=[HTTPMethod.GET], path=path, middlewares=[mw])
    async def handler(scope, receive, send):
        return 'ok'

    fn = router[(path, HTTPMethod.GET, Scheme.http)]
    assert await fn({}, None, None) == 'ok'


@pytest.mark.asyncio
async def test_middlewares_chain_runs_in_order(router):
    path = 'test_mw_order'
    log = []

    async def mw1(scope, receive, send, call_next):
        log.append('mw1_before')
        res = await call_next(scope, receive, send)
        log.append('mw1_after')
        return res

    async def mw2(scope, receive, send, call_next):
        log.append('mw2_before')
        res = await call_next(scope, receive, send)
        log.append('mw2_after')
        return res

    @router.route(methods=[HTTPMethod.GET], path=path, middlewares=[mw1, mw2])
    async def handler(scope, receive, send):
        log.append('handler')
        return 'done'

    fn = router[(path, HTTPMethod.GET, Scheme.http)]
    await fn({}, None, None)
    assert log == ['mw1_before', 'mw2_before', 'handler', 'mw2_after', 'mw1_after']


@pytest.mark.asyncio
async def test_middlewares_short_circuits(router):
    path = 'test_mw_sc'
    called = []

    async def guard(scope, receive, send, call_next):
        called.append('guard')
        return 'blocked'

    @router.route(methods=[HTTPMethod.GET], path=path, middlewares=[guard])
    async def handler(scope, receive, send):
        called.append('handler')
        return 'ok'

    fn = router[(path, HTTPMethod.GET, Scheme.http)]
    result = await fn({}, None, None)
    assert result == 'blocked'
    assert 'handler' not in called


def test_middlewares_handler_returned_unchanged(router):
    path = 'test_mw_ret'

    async def mw(scope, receive, send, call_next):
        return await call_next(scope, receive, send)

    @router.route(methods=[HTTPMethod.GET], path=path, middlewares=[mw])
    async def handler(scope, receive, send):
        return 'ok'

    assert callable(handler)


@pytest.mark.asyncio
async def test_functions_path_unchanged(router):
    path = 'test_fn_path'

    async def mw(scope, receive, send, inner):
        res = await inner(scope, receive, send)
        return res + '_mw'

    async def handler(scope, receive, send):
        return 'ok'

    router.route(methods=[HTTPMethod.GET], path=path, functions=[mw, handler])
    fn = router[(path, HTTPMethod.GET, Scheme.http)]
    assert await fn({}, None, None) == 'ok_mw'


# ---------------------------------------------------------------------------
# RouteGroup / BlackBull.group() tests
# ---------------------------------------------------------------------------

@pytest.fixture
def app_():
    return BlackBull()


@pytest.mark.asyncio
async def test_group_middleware_runs_for_every_route(app_):
    log = []

    async def group_mw(scope, receive, send, call_next):
        log.append('group_mw')
        return await call_next(scope, receive, send)

    grp = app_.group(middlewares=[group_mw])

    @grp.route(methods=[HTTPMethod.GET], path='/a')
    async def route_a(scope, receive, send):
        return 'a'

    @grp.route(methods=[HTTPMethod.GET], path='/b')
    async def route_b(scope, receive, send):
        return 'b'

    scope_a = {'type': 'http', 'method': 'GET', 'path': '/a', 'headers': {}}
    scope_b = {'type': 'http', 'method': 'GET', 'path': '/b', 'headers': {}}

    results = []
    async def capture(event, *a, **kw):
        results.append(event)

    await app_(scope_a, None, capture)
    await app_(scope_b, None, capture)
    assert log == ['group_mw', 'group_mw']


@pytest.mark.asyncio
async def test_group_per_route_middleware_appended_after_group(app_):
    order = []

    async def group_mw(scope, receive, send, call_next):
        order.append('group')
        return await call_next(scope, receive, send)

    async def route_mw(scope, receive, send, call_next):
        order.append('route')
        return await call_next(scope, receive, send)

    grp = app_.group(middlewares=[group_mw])

    @grp.route(methods=[HTTPMethod.GET], path='/x', middlewares=[route_mw])
    async def handler(scope, receive, send):
        order.append('handler')
        return 'ok'

    scope = {'type': 'http', 'method': 'GET', 'path': '/x', 'headers': {}}
    await app_(scope, None, lambda *a, **kw: None)
    assert order == ['group', 'route', 'handler']


@pytest.mark.asyncio
async def test_group_short_circuit_prevents_handler(app_):
    called = []

    async def guard(scope, receive, send, call_next):
        called.append('guard')
        return 'blocked'

    grp = app_.group(middlewares=[guard])

    @grp.route(methods=[HTTPMethod.GET], path='/guarded')
    async def handler(scope, receive, send):
        called.append('handler')
        return 'ok'

    scope = {'type': 'http', 'method': 'GET', 'path': '/guarded', 'headers': {}}
    await app_(scope, None, lambda *a, **kw: None)
    assert 'handler' not in called
    assert called == ['guard']


@pytest.mark.asyncio
async def test_group_routes_independent_of_non_group_routes(app_):
    log = []

    async def group_mw(scope, receive, send, call_next):
        log.append('group_mw')
        return await call_next(scope, receive, send)

    grp = app_.group(middlewares=[group_mw])

    @grp.route(methods=[HTTPMethod.GET], path='/protected')
    async def protected(scope, receive, send):
        return 'protected'

    @app_.route(methods=[HTTPMethod.GET], path='/public')
    async def public(scope, receive, send):
        return 'public'

    scope_pub = {'type': 'http', 'method': 'GET', 'path': '/public', 'headers': {}}
    await app_(scope_pub, None, lambda *a, **kw: None)
    assert log == []  # group_mw must NOT fire for /public


# ---------------------------------------------------------------------------
# Path param injection into scope for middleware chains
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_middleware_chain_path_param_injected_into_scope(router):
    path = 'items/{item_id}'
    seen = {}

    async def mw(scope, receive, send, call_next):
        return await call_next(scope, receive, send)

    async def handler(scope, receive, send):
        seen.update(scope.get('path_params', {}))
        return 'ok'

    router.route(methods=[HTTPMethod.GET], path=path, middlewares=[mw])
    # Override with middleware chain:
    router.route(methods=[HTTPMethod.GET], path=path, functions=[mw, handler])
    fn = router[('items/42', HTTPMethod.GET, Scheme.http)]
    await fn({'headers': {}}, None, None)
    assert seen.get('item_id') == '42'


@pytest.mark.asyncio
async def test_plain_route_path_param_in_scope(router):
    path = 'plain/{value}'

    @router.route(path=path, methods=[HTTPMethod.GET])
    async def fn(scope, receive, send):
        pass

    f = router[('plain/hello', HTTPMethod.GET, Scheme.http)]
    scope = {}
    await f(scope, None, None)
    assert scope['path_params']['value'] == 'hello'
