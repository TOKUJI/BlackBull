from logging import getLogger
from http import HTTPStatus, HTTPMethod
from unittest.mock import AsyncMock
# import asyncio
import pytest

# Test targets
from blackbull.utils import Scheme
from blackbull.router import Router, _middleware_param, has_middleware_param, _is_simplified_handler, _adapt_handler
from blackbull import BlackBull, Response, JSONResponse
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


# ---------------------------------------------------------------------------
# Simplified handler tests
# ---------------------------------------------------------------------------

class TestSimplifiedHandlerDetection:
    def test_full_asgi_not_simplified(self):
        async def fn(scope, receive, send): pass
        assert _is_simplified_handler(fn) is False

    def test_middleware_not_simplified(self):
        async def fn(scope, receive, send, call_next): pass
        assert _is_simplified_handler(fn) is False

    def test_middleware_inner_not_simplified(self):
        async def fn(scope, receive, send, inner): pass
        assert _is_simplified_handler(fn) is False

    def test_no_params_is_simplified(self):
        async def fn(): pass
        assert _is_simplified_handler(fn) is True

    def test_path_param_only_is_simplified(self):
        async def fn(task_id): pass
        assert _is_simplified_handler(fn) is True

    def test_body_only_is_simplified(self):
        async def fn(body: bytes): pass
        assert _is_simplified_handler(fn) is True

    def test_scope_only_is_simplified(self):
        async def fn(scope): pass
        assert _is_simplified_handler(fn) is True

    def test_partial_asgi_is_simplified(self):
        # Has 'scope' but not 'receive'/'send'
        async def fn(scope, receive): pass
        assert _is_simplified_handler(fn) is True


class TestSimplifiedHandlerFailFast:
    def test_unknown_param_raises_at_registration(self):
        async def fn(x): pass
        with pytest.raises(TypeError, match="cannot resolve parameter 'x'"):
            _adapt_handler(fn, '/items/{id}')

    def test_unknown_param_raises_when_not_a_path_param(self):
        async def fn(name): pass
        with pytest.raises(TypeError, match="cannot resolve parameter 'name'"):
            # path has {id}, not {name}
            _adapt_handler(fn, '/items/{id}')


class TestSimplifiedHandlerPathParams:
    @pytest.mark.asyncio
    async def test_no_params(self):
        send = AsyncMock()
        async def fn(): return Response(b'hello')
        wrapper = _adapt_handler(fn, '/')
        await wrapper({}, None, send)
        send.assert_called_once()

    @pytest.mark.asyncio
    async def test_path_param_str_no_annotation(self):
        captured = {}
        async def fn(task_id): captured['task_id'] = task_id
        wrapper = _adapt_handler(fn, '/tasks/{task_id}')
        await wrapper({'path_params': {'task_id': '42'}}, None, AsyncMock())
        assert captured['task_id'] == '42'
        assert isinstance(captured['task_id'], str)

    @pytest.mark.asyncio
    async def test_path_param_int_annotation(self):
        captured = {}
        async def fn(task_id: int): captured['task_id'] = task_id
        wrapper = _adapt_handler(fn, '/tasks/{task_id}')
        await wrapper({'path_params': {'task_id': '42'}}, None, AsyncMock())
        assert captured['task_id'] == 42
        assert isinstance(captured['task_id'], int)

    @pytest.mark.asyncio
    async def test_multiple_path_params(self):
        captured = {}
        async def fn(x, y: int): captured.update({'x': x, 'y': y})
        wrapper = _adapt_handler(fn, '/a/{x}/{y}')
        await wrapper({'path_params': {'x': 'hello', 'y': '7'}}, None, AsyncMock())
        assert captured == {'x': 'hello', 'y': 7}

    @pytest.mark.asyncio
    async def test_path_param_coercion_error(self):
        async def fn(task_id: int): pass
        wrapper = _adapt_handler(fn, '/tasks/{task_id}')
        with pytest.raises(TypeError, match="cannot coerce"):
            await wrapper({'path_params': {'task_id': 'notanint'}}, None, AsyncMock())

    @pytest.mark.asyncio
    async def test_scope_escape_hatch(self):
        captured = {}
        async def fn(scope): captured['scope'] = scope
        wrapper = _adapt_handler(fn, '/')
        fake_scope = {'type': 'http', 'method': 'GET'}
        await wrapper(fake_scope, None, AsyncMock())
        assert captured['scope'] is fake_scope


class TestSimplifiedHandlerBodyInjection:
    @pytest.mark.asyncio
    async def test_body_injected(self):
        captured = {}
        async def fn(body: bytes): captured['body'] = body

        async def fake_receive():
            return {'type': 'http.request', 'body': b'hello', 'more_body': False}

        wrapper = _adapt_handler(fn, '/')
        await wrapper({}, fake_receive, AsyncMock())
        assert captured['body'] == b'hello'

    @pytest.mark.asyncio
    async def test_no_body_param_does_not_call_receive(self):
        async def fn(task_id): pass

        called = []
        async def should_not_be_called():
            called.append(True)
            return {'type': 'http.request', 'body': b'', 'more_body': False}

        wrapper = _adapt_handler(fn, '/tasks/{task_id}')
        await wrapper({'path_params': {'task_id': '1'}}, should_not_be_called, AsyncMock())
        assert called == []


class TestSimplifiedHandlerReturnValues:
    @pytest.mark.asyncio
    async def test_returns_response(self):
        send = AsyncMock()
        resp = Response(b'ok')
        async def fn(): return resp
        wrapper = _adapt_handler(fn, '/')
        await wrapper({}, None, send)
        send.assert_called_once_with(resp)

    @pytest.mark.asyncio
    async def test_returns_bytes(self):
        send = AsyncMock()
        async def fn(): return b'hello'
        wrapper = _adapt_handler(fn, '/')
        await wrapper({}, None, send)
        send.assert_called_once()
        arg = send.call_args[0][0]
        assert isinstance(arg, Response)

    @pytest.mark.asyncio
    async def test_returns_str(self):
        send = AsyncMock()
        async def fn(): return 'hello'
        wrapper = _adapt_handler(fn, '/')
        await wrapper({}, None, send)
        send.assert_called_once()
        arg = send.call_args[0][0]
        assert isinstance(arg, Response)

    @pytest.mark.asyncio
    async def test_returns_dict(self):
        send = AsyncMock()
        async def fn(): return {'key': 'value'}
        wrapper = _adapt_handler(fn, '/')
        await wrapper({}, None, send)
        send.assert_called_once()
        arg = send.call_args[0][0]
        assert isinstance(arg, JSONResponse)

    @pytest.mark.asyncio
    async def test_returns_none_no_send(self):
        send = AsyncMock()
        async def fn(): return None
        wrapper = _adapt_handler(fn, '/')
        await wrapper({}, None, send)
        send.assert_not_called()

    @pytest.mark.asyncio
    async def test_returns_unsupported_type_raises(self):
        async def fn(): return 42
        wrapper = _adapt_handler(fn, '/')
        with pytest.raises(TypeError, match="unsupported type"):
            await wrapper({}, None, AsyncMock())


class TestSimplifiedHandlerSyncAndAsync:
    @pytest.mark.asyncio
    async def test_sync_handler(self):
        send = AsyncMock()
        def fn(task_id: int): return Response(str(task_id).encode())
        wrapper = _adapt_handler(fn, '/tasks/{task_id}')
        await wrapper({'path_params': {'task_id': '7'}}, None, send)
        send.assert_called_once()
        arg = send.call_args[0][0]
        assert isinstance(arg, Response)

    @pytest.mark.asyncio
    async def test_async_handler(self):
        send = AsyncMock()
        async def fn(task_id: int): return Response(str(task_id).encode())
        wrapper = _adapt_handler(fn, '/tasks/{task_id}')
        await wrapper({'path_params': {'task_id': '7'}}, None, send)
        send.assert_called_once()


class TestSimplifiedHandlerRegistration:
    @pytest.mark.asyncio
    async def test_route_fn_path(self, router):
        """Simplified handler registered via plain @router.route() decorator."""
        @router.route(path='/tasks/{task_id}', methods=[HTTPMethod.GET])
        async def get_task(task_id):
            return Response(task_id.encode())

        send = AsyncMock()
        fn = router[('/tasks/99', HTTPMethod.GET, Scheme.http)]
        await fn({'path_params': {'task_id': '99'}}, None, send)
        send.assert_called_once()

    @pytest.mark.asyncio
    async def test_no_params_route(self, router):
        """Handler with no params at all."""
        @router.route(path='/', methods=[HTTPMethod.GET])
        async def index():
            return Response(b'Hello')

        send = AsyncMock()
        fn = router[('/', HTTPMethod.GET, Scheme.http)]
        await fn({}, None, send)
        send.assert_called_once()

    @pytest.mark.asyncio
    async def test_middlewares_branch(self, router):
        """Simplified handler at the end of a middlewares=[...] chain."""
        seen = []

        async def mw(scope, receive, send, call_next):
            seen.append('mw')
            await call_next(scope, receive, send)

        @router.route(path='/tasks/{task_id}', methods=[HTTPMethod.GET],
                      middlewares=[mw])
        async def get_task(task_id):
            seen.append(task_id)
            return Response(task_id.encode())

        send = AsyncMock()
        fn = router[('/tasks/5', HTTPMethod.GET, Scheme.http)]
        await fn({'path_params': {'task_id': '5'}}, None, send)
        assert seen == ['mw', '5']
        send.assert_called_once()

    @pytest.mark.asyncio
    async def test_functions_branch(self, router):
        """Simplified handler as the last item in functions=[...]."""
        seen = []

        async def mw(scope, receive, send, call_next):
            seen.append('mw')
            await call_next(scope, receive, send)

        async def get_task(task_id: int):
            seen.append(task_id)
            return Response(str(task_id).encode())

        router.route(path='/tasks/{task_id}', methods=[HTTPMethod.GET],
                     functions=[mw, get_task])

        send = AsyncMock()
        fn = router[('/tasks/3', HTTPMethod.GET, Scheme.http)]
        await fn({'path_params': {'task_id': '3'}}, None, send)
        assert seen == ['mw', 3]
        send.assert_called_once()

    def test_full_asgi_handler_unchanged(self, router):
        """Full ASGI handler still passes through _is_simplified_handler as False."""
        async def full(scope, receive, send): pass
        assert _is_simplified_handler(full) is False
        # Registering it should work without any wrapping
        router.route(path='/full', methods=[HTTPMethod.GET])(full)
        fn = router[('/full', HTTPMethod.GET, Scheme.http)]
        assert fn is not None
