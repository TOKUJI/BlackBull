import json
import re
from logging import getLogger
from http import HTTPStatus, HTTPMethod
from unittest.mock import AsyncMock
# import asyncio
import pytest

# Runtime type checks come from beartype's import-hook (claw); violations
# of an annotation raise BeartypeCallHintParamViolation, a TypeError subclass.
# We keep the optional-import dance so this file still runs when beartype
# isn't installed (e.g. minimal-deps smoke runs).
try:
    from beartype.roar import BeartypeCallHintParamViolation as _BeartypeViolation
except ImportError:
    _BeartypeViolation = None  # type: ignore[assignment,misc]

_TYPE_ERRORS = (TypeError,) if _BeartypeViolation is None else (TypeError, _BeartypeViolation)

# Test targets
from blackbull.utils import Scheme
import uuid
from blackbull.router import (
    Router, ErrorRouter,
    _middleware_param, has_middleware_param,
    _is_simplified_handler, _adapt_handler,
    PathNotRegistered, MethodNotApplicable, ConfigurationError, HTTPException,
)
from blackbull import BlackBull, Response, JSONResponse
from blackbull.request import Request
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
    path = re.compile(r'^test/\d+$')
    scheme = Scheme.http

    @router.route(path=path, methods=[HTTPMethod.GET])
    def fn(*args, **kwargs):
        pass

    f = router[('test/1234', HTTPMethod.GET, scheme)]

    assert f == fn


@pytest.mark.asyncio
async def test_router_regex_with_group_name1(router):
    # Custom regex routes take a compiled re.Pattern (the documented
    # contract).  A raw regex *string* is rejected at registration since
    # Sprint 64 — see test_router_regex_source_string_rejected.
    path = re.compile(r'^test/(?P<id_>\d+)$')
    scheme = Scheme.http

    @router.route(path=path, methods=[HTTPMethod.GET])
    async def fn(scope, receive, send):
        pass

    f = router[('test/1234', HTTPMethod.GET, scheme)]
    scope = {}
    await f(scope, None, None)
    assert scope['path_params']['id_'] == '1234'


def test_router_regex_source_string_rejected(router):
    """A regex source passed as a *string* path must fail loudly at
    registration (it would otherwise register as a literal path and 404)."""
    with pytest.raises(ValueError, match='regex metacharacters'):
        @router.route(path=r'^test/(?P<id_>\d+)$', methods=[HTTPMethod.GET])
        async def fn(scope, receive, send):
            pass


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
    # Spec change (Sprint 74): a scalar param that matches no path
    # placeholder is now a *query param*, not a registration error — the
    # fail-fast contract holds only for annotations no category can resolve.
    def test_unresolvable_annotation_raises_at_registration(self):
        async def fn(x: dict): pass
        with pytest.raises(TypeError, match="cannot resolve parameter 'x'"):
            _adapt_handler(fn, '/items/{id}')

    def test_container_annotation_raises_at_registration(self):
        async def fn(name: list[str]): pass
        with pytest.raises(TypeError, match="cannot resolve parameter 'name'"):
            # path has {id}, not {name} — and list[str] is not a query scalar
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


class TestSimplifiedHandlerRequestInjection:
    """Injection matrix for the opt-in Request context object (Sprint 65)."""

    @staticmethod
    def _counting_receive(body: bytes = b'hello'):
        calls = []

        async def receive():
            calls.append(True)
            return {'type': 'http.request', 'body': body, 'more_body': False}

        return receive, calls

    @pytest.mark.asyncio
    async def test_request_injected_by_annotation(self):
        captured = {}
        async def fn(request: Request): captured['request'] = request
        wrapper = _adapt_handler(fn, '/')
        scope = {'type': 'http', 'method': 'GET', 'path': '/', 'headers': []}
        await wrapper(scope, None, AsyncMock())
        assert isinstance(captured['request'], Request)
        assert captured['request'].scope is scope

    @pytest.mark.asyncio
    async def test_request_injected_by_bare_name(self):
        captured = {}
        async def fn(request): captured['request'] = request
        wrapper = _adapt_handler(fn, '/')
        await wrapper({'headers': []}, None, AsyncMock())
        assert isinstance(captured['request'], Request)

    @pytest.mark.asyncio
    async def test_request_annotation_under_any_name(self):
        captured = {}
        async def fn(req: Request): captured['req'] = req
        wrapper = _adapt_handler(fn, '/')
        await wrapper({'headers': []}, None, AsyncMock())
        assert isinstance(captured['req'], Request)

    @pytest.mark.asyncio
    async def test_request_and_body_share_single_drain(self):
        captured = {}
        async def fn(request: Request, body: bytes):
            captured['body'] = body
            captured['via_request'] = await request.body()

        receive, calls = self._counting_receive(b'payload')
        wrapper = _adapt_handler(fn, '/')
        await wrapper({'headers': []}, receive, AsyncMock())
        assert captured['body'] == b'payload'
        assert captured['via_request'] == b'payload'
        assert len(calls) == 1

    @pytest.mark.asyncio
    async def test_request_with_path_params(self):
        captured = {}
        async def fn(task_id: int, request: Request):
            captured.update({'task_id': task_id, 'request': request})
        wrapper = _adapt_handler(fn, '/tasks/{task_id}')
        await wrapper({'path_params': {'task_id': '42'}, 'headers': []}, None, AsyncMock())
        assert captured['task_id'] == 42
        assert isinstance(captured['request'], Request)

    @pytest.mark.asyncio
    async def test_request_with_scope(self):
        captured = {}
        async def fn(scope, request: Request):
            captured.update({'scope': scope, 'request': request})
        wrapper = _adapt_handler(fn, '/')
        fake_scope = {'type': 'http', 'headers': []}
        await wrapper(fake_scope, None, AsyncMock())
        assert captured['scope'] is fake_scope
        assert captured['request'].scope is fake_scope

    @pytest.mark.asyncio
    async def test_request_with_dataclass_body_single_drain(self):
        from dataclasses import dataclass

        @dataclass
        class Item:
            name: str

        captured = {}
        async def fn(item: Item, request: Request):
            captured['item'] = item
            captured['raw'] = await request.body()

        receive, calls = self._counting_receive(b'{"name": "spanner"}')
        wrapper = _adapt_handler(fn, '/')
        await wrapper({'headers': []}, receive, AsyncMock())
        assert captured['item'] == Item(name='spanner')
        assert captured['raw'] == b'{"name": "spanner"}'
        assert len(calls) == 1

    def test_request_name_with_foreign_annotation_is_not_request_injected(self):
        # Spec change (Sprint 74): 'request' with a scalar annotation used to
        # be a registration TypeError; it is now an ordinary query param
        # (asserted in test_query_params.py).  An unresolvable annotation on
        # the name still fails fast — proving no Request fallback kicks in.
        async def fn(request: dict): pass
        with pytest.raises(TypeError, match="cannot resolve parameter 'request'"):
            _adapt_handler(fn, '/')

    @pytest.mark.asyncio
    async def test_no_request_param_never_constructs_request(self):
        # Zero-cost check at the observable level: a handler without a
        # Request param must not touch receive, and the raw scope object is
        # handed through untouched (no wrapper dict).
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


# ---------------------------------------------------------------------------
# Simplified handler — dataclass body deserialization (v3)
# ---------------------------------------------------------------------------

from dataclasses import dataclass, field as _dc_field


@dataclass
class _Item:
    name: str
    qty: int = 1


@dataclass
class _Address:
    street: str
    zip: str


@dataclass
class _Person:
    name: str
    home: _Address
    tags: list[str] = _dc_field(default_factory=list)


class TestDataclassBodyDeserialization:
    """When a handler parameter is annotated with a dataclass, the router
    reads the request body, parses it as JSON, and constructs an instance.
    """

    def _receive_with(self, body: bytes):
        """Build an ASGI ``receive`` coroutine that delivers ``body`` once,
        then a disconnect.
        """
        events = iter([
            {'type': 'http.request', 'body': body, 'more_body': False},
            {'type': 'http.disconnect'},
        ])
        async def receive():
            return next(events)
        return receive

    @pytest.mark.asyncio
    async def test_simple_dataclass_body(self):
        captured = {}
        async def fn(body: _Item):
            captured['body'] = body
        wrapper = _adapt_handler(fn, '/items')
        await wrapper({}, self._receive_with(b'{"name":"widget","qty":3}'), AsyncMock())
        assert captured['body'] == _Item(name='widget', qty=3)

    @pytest.mark.asyncio
    async def test_parameter_name_other_than_body(self):
        """The dataclass annotation — not the parameter name — drives
        body detection.  ``item: _Item`` works as well as ``body: _Item``."""
        captured = {}
        async def fn(item: _Item):
            captured['item'] = item
        wrapper = _adapt_handler(fn, '/items')
        await wrapper({}, self._receive_with(b'{"name":"x","qty":7}'), AsyncMock())
        assert captured['item'] == _Item(name='x', qty=7)

    @pytest.mark.asyncio
    async def test_missing_optional_field_uses_default(self):
        captured = {}
        async def fn(body: _Item):
            captured['body'] = body
        wrapper = _adapt_handler(fn, '/items')
        await wrapper({}, self._receive_with(b'{"name":"only-name"}'), AsyncMock())
        assert captured['body'] == _Item(name='only-name', qty=1)

    @pytest.mark.asyncio
    async def test_missing_required_field_raises(self):
        """A body missing a required field is a client error → 400 (bug 1.12)."""
        async def fn(body: _Item): pass
        wrapper = _adapt_handler(fn, '/items')
        with pytest.raises(HTTPException) as exc_info:
            await wrapper({}, self._receive_with(b'{"qty":3}'), AsyncMock())
        assert exc_info.value.status == HTTPStatus.BAD_REQUEST

    @pytest.mark.asyncio
    async def test_unknown_field_raises(self):
        """An unknown body field is a client error → 400 (bug 1.12)."""
        async def fn(body: _Item): pass
        wrapper = _adapt_handler(fn, '/items')
        with pytest.raises(HTTPException, match='Unknown field') as exc_info:
            await wrapper({}, self._receive_with(b'{"name":"x","extra":1}'), AsyncMock())
        assert exc_info.value.status == HTTPStatus.BAD_REQUEST

    @pytest.mark.asyncio
    async def test_nested_dataclass(self):
        captured = {}
        async def fn(body: _Person):
            captured['body'] = body
        wrapper = _adapt_handler(fn, '/people')
        await wrapper({}, self._receive_with(
            b'{"name":"alice","home":{"street":"1 Main","zip":"00000"},"tags":["a","b"]}'
        ), AsyncMock())
        assert captured['body'] == _Person(
            name='alice',
            home=_Address(street='1 Main', zip='00000'),
            tags=['a', 'b'],
        )

    @pytest.mark.asyncio
    async def test_list_of_dataclass_field(self):
        @dataclass
        class Cart:
            items: list[_Item]

        captured = {}
        async def fn(body: Cart):
            captured['body'] = body
        wrapper = _adapt_handler(fn, '/cart')
        await wrapper({}, self._receive_with(
            b'{"items":[{"name":"a","qty":1},{"name":"b","qty":2}]}'
        ), AsyncMock())
        assert captured['body'] == Cart(items=[_Item('a', 1), _Item('b', 2)])

    @pytest.mark.asyncio
    async def test_optional_field(self):
        @dataclass
        class Maybe:
            value: int | None = None

        captured = {}
        async def fn(body: Maybe):
            captured['body'] = body
        wrapper = _adapt_handler(fn, '/maybe')
        await wrapper({}, self._receive_with(b'{"value":null}'), AsyncMock())
        assert captured['body'] == Maybe(value=None)

        await wrapper({}, self._receive_with(b'{"value":42}'), AsyncMock())
        assert captured['body'] == Maybe(value=42)

    @pytest.mark.asyncio
    async def test_two_body_params_rejected_at_registration(self):
        async def fn(a: _Item, b: _Item): pass
        with pytest.raises(TypeError, match='more than one parameter'):
            _adapt_handler(fn, '/x')

    @pytest.mark.asyncio
    async def test_invalid_json_raises(self):
        """Malformed JSON in the body is a client error → 400 (bug 1.12),
        not the raw JSONDecodeError that used to surface as a 500."""
        async def fn(body: _Item): pass
        wrapper = _adapt_handler(fn, '/items')
        with pytest.raises(HTTPException) as exc_info:
            await wrapper({}, self._receive_with(b'not json'), AsyncMock())
        assert exc_info.value.status == HTTPStatus.BAD_REQUEST

    @pytest.mark.asyncio
    async def test_bytes_body_still_works(self):
        """The pre-v3 behaviour — ``body: bytes`` — keeps delivering raw bytes."""
        captured = {}
        async def fn(body: bytes):
            captured['body'] = body
        wrapper = _adapt_handler(fn, '/raw')
        await wrapper({}, self._receive_with(b'{"name":"x"}'), AsyncMock())
        assert captured['body'] == b'{"name":"x"}'

    @pytest.mark.asyncio
    async def test_handler_returns_dataclass_serialized_as_json(self):
        sent = []
        async def fake_send(event):
            sent.append(event)

        async def fn() -> _Item:
            return _Item(name='widget', qty=5)
        wrapper = _adapt_handler(fn, '/')
        await wrapper({}, AsyncMock(), fake_send)
        # The simplified-handler adapter forwards the JSONResponse object to
        # send() — the next layer (server / framework) is what splits it into
        # ASGI events.  Just check the Response payload.
        assert len(sent) == 1
        resp = sent[0]
        from blackbull import JSONResponse
        assert isinstance(resp, JSONResponse)
        assert resp.body == b'{"name": "widget", "qty": 5}'

    @pytest.mark.asyncio
    async def test_handler_returns_list_of_dataclasses(self):
        sent = []
        async def fake_send(event):
            sent.append(event)

        async def fn() -> list[_Item]:
            return [_Item('a', 1), _Item('b', 2)]
        wrapper = _adapt_handler(fn, '/')
        await wrapper({}, AsyncMock(), fake_send)
        assert json.loads(sent[0].body) == [
            {'name': 'a', 'qty': 1},
            {'name': 'b', 'qty': 2},
        ]

    @pytest.mark.asyncio
    async def test_path_param_takes_precedence_over_body(self):
        """A parameter whose name matches a {placeholder} in the path is a
        path param, even if it happens to be annotated as a dataclass."""
        captured = {}
        async def fn(item: int):
            captured['item'] = item
        wrapper = _adapt_handler(fn, '/items/{item}')
        await wrapper({'path_params': {'item': '7'}}, AsyncMock(), AsyncMock())
        assert captured['item'] == 7


# ---------------------------------------------------------------------------
# Router edge-case tests
# ---------------------------------------------------------------------------

class TestRouterEdgeCases:
    def test_setitem_two_tuple_key(self):
        router = Router()
        async def fn(scope, receive, send): pass
        router[('/two', HTTPMethod.GET)] = fn
        # Must be reachable with any scheme
        result = router[('/two', HTTPMethod.GET, Scheme.http)]
        assert result is not None

    def test_setitem_regex_pattern(self):
        router = Router()
        async def fn(scope, receive, send): pass
        pattern = re.compile(r'^/api/\d+$')
        router[(pattern, HTTPMethod.GET, Scheme.http)] = fn
        # Stored as a raw-regex route, not a string path
        assert any(k[0] is pattern for k in router._raw_regex)
        assert not router._string_paths

    def test_getitem_scheme_mismatch_raises(self):
        router = Router()
        async def fn(scope, receive, send): pass
        router[('/only-ws', HTTPMethod.GET, Scheme.websocket)] = fn
        with pytest.raises(PathNotRegistered):
            router[('/only-ws', HTTPMethod.GET, Scheme.http)]

    def test_contains_regex_pattern(self):
        router = Router()
        async def fn(scope, receive, send): pass
        router[(re.compile(r'^/api/\d+$'), HTTPMethod.GET, Scheme.http)] = fn
        assert '/api/42' in router
        assert '/other' not in router

    def test_repr_is_string(self):
        router = Router()
        r = repr(router)
        assert isinstance(r, str)
        assert 'Router' in r

    def test_route_fn_invalid_method_token_raises(self):
        """Method strings must be valid RFC 9110 §5.6.2 tokens.

        Valid str methods like 'BREW' are accepted (Sprint 47).
        Strings containing spaces, control characters, or separator
        characters are not valid tokens and must raise ValueError.
        """
        router = Router()
        with pytest.raises(ValueError):
            router.route_fn(methods=['BREW METHOD'], path='/x')  # space is not tchar

    def test_register_chain_non_middleware_in_middle_raises(self):
        router = Router()

        async def plain_fn(scope, receive, send): pass  # no call_next

        async def terminal(scope, receive, send): pass

        with pytest.raises(ValueError):
            router._register_chain([plain_fn, terminal], '/x', [HTTPMethod.GET], Scheme.http)


# ---------------------------------------------------------------------------
# ErrorRouter edge-case tests
# ---------------------------------------------------------------------------

class TestErrorRouterEdgeCases:
    def test_setitem_invalid_key_raises_typeerror(self):
        er = ErrorRouter()
        with pytest.raises(_TYPE_ERRORS):
            er[42] = lambda: None

    def test_getitem_invalid_key_raises_typeerror(self):
        er = ErrorRouter()
        with pytest.raises(_TYPE_ERRORS):
            _ = er[42]

    def test_setitem_non_error_status_raises_valueerror(self):
        er = ErrorRouter()
        with pytest.raises(ValueError):
            er[HTTPStatus.OK] = lambda: None


# ---------------------------------------------------------------------------
# Type converter tests
# ---------------------------------------------------------------------------

class TestConverterRouting:
    @pytest.mark.asyncio
    async def test_int_converter_injects_int(self):
        router = Router()

        @router.route(path='/items/{id:int}', methods=HTTPMethod.GET)
        async def handler(scope, receive, send):
            pass

        scope = {'path_params': {}}
        fn = router[('/items/42', HTTPMethod.GET, Scheme.http)]
        await fn(scope, None, None)
        assert scope['path_params']['id'] == 42
        assert isinstance(scope['path_params']['id'], int)

    @pytest.mark.asyncio
    async def test_uuid_converter_injects_uuid(self):
        router = Router()
        uid = '12345678-1234-5678-1234-567812345678'

        @router.route(path='/users/{uid:uuid}', methods=HTTPMethod.GET)
        async def handler(scope, receive, send):
            pass

        scope = {'path_params': {}}
        fn = router[(f'/users/{uid}', HTTPMethod.GET, Scheme.http)]
        await fn(scope, None, None)
        assert scope['path_params']['uid'] == uuid.UUID(uid)

    @pytest.mark.asyncio
    async def test_path_converter_matches_slashes(self):
        router = Router()

        @router.route(path='/files/{rest:path}', methods=HTTPMethod.GET)
        async def handler(scope, receive, send):
            pass

        scope = {'path_params': {}}
        fn = router[('/files/a/b/c.txt', HTTPMethod.GET, Scheme.http)]
        await fn(scope, None, None)
        assert scope['path_params']['rest'] == 'a/b/c.txt'

    def test_unknown_converter_raises_at_registration(self):
        router = Router()

        async def fn(scope, receive, send): pass

        with pytest.raises(ValueError, match="Unknown converter 'foo'"):
            router[('/x/{id:foo}', HTTPMethod.GET, Scheme.http)] = fn

    @pytest.mark.asyncio
    async def test_str_converter_is_default(self):
        router = Router()

        @router.route(path='/things/{name}', methods=HTTPMethod.GET)
        async def handler(scope, receive, send):
            pass

        scope = {'path_params': {}}
        fn = router[('/things/hello', HTTPMethod.GET, Scheme.http)]
        await fn(scope, None, None)
        assert scope['path_params']['name'] == 'hello'
        assert isinstance(scope['path_params']['name'], str)


class TestConverterSimplifiedHandler:
    @pytest.mark.asyncio
    async def test_int_converter_with_simplified_handler(self):
        router = Router()

        @router.route(path='/items/{id:int}', methods=HTTPMethod.GET)
        async def handler(id: int):
            return str(id * 2)

        scope = {'type': 'http', 'path_params': {}}
        send_calls = []

        async def fake_send(data, status=None, headers=None):
            send_calls.append(data)

        fn = router[('/items/21', HTTPMethod.GET, Scheme.http)]
        await fn(scope, None, fake_send)
        assert b'42' in send_calls[0].body

    @pytest.mark.asyncio
    async def test_uuid_converter_with_simplified_handler(self):
        router = Router()
        uid = '12345678-1234-5678-1234-567812345678'

        @router.route(path='/users/{uid:uuid}', methods=HTTPMethod.GET)
        async def handler(uid: uuid.UUID):
            return str(uid)

        scope = {'type': 'http', 'path_params': {}}
        send_calls = []

        async def fake_send(data, status=None, headers=None):
            send_calls.append(data)

        fn = router[(f'/users/{uid}', HTTPMethod.GET, Scheme.http)]
        await fn(scope, None, fake_send)
        assert uid.encode() in send_calls[0].body


# ---------------------------------------------------------------------------
# url_path_for tests
# ---------------------------------------------------------------------------

class TestUrlPathFor:
    def test_simple_substitution(self):
        router = Router()

        @router.route(path='/items/{id:int}', methods=HTTPMethod.GET, name='item-detail')
        async def handler(scope, receive, send): pass

        assert router.url_path_for('item-detail', id=42) == '/items/42'

    def test_no_params(self):
        router = Router()

        @router.route(path='/about', methods=HTTPMethod.GET, name='about')
        async def handler(scope, receive, send): pass

        assert router.url_path_for('about') == '/about'

    def test_multiple_params(self):
        router = Router()

        @router.route(path='/a/{x}/b/{y}', methods=HTTPMethod.GET, name='nested')
        async def handler(scope, receive, send): pass

        assert router.url_path_for('nested', x='foo', y='bar') == '/a/foo/b/bar'

    def test_unknown_name_raises_keyerror(self):
        router = Router()
        with pytest.raises(KeyError, match="nope"):
            router.url_path_for('nope')

    def test_missing_param_raises_valueerror(self):
        router = Router()

        @router.route(path='/items/{id:int}', methods=HTTPMethod.GET, name='item')
        async def handler(scope, receive, send): pass

        with pytest.raises(ValueError, match="missing params"):
            router.url_path_for('item')

    def test_duplicate_name_raises_valueerror(self):
        router = Router()

        @router.route(path='/a', methods=HTTPMethod.GET, name='dup')
        async def h1(scope, receive, send): pass

        with pytest.raises(ValueError, match="Duplicate route name"):
            @router.route(path='/b', methods=HTTPMethod.GET, name='dup')
            async def h2(scope, receive, send): pass


# ---------------------------------------------------------------------------
# Startup validation tests
# ---------------------------------------------------------------------------

class TestStartupValidation:
    def test_valid_routes_pass(self):
        router = Router()

        @router.route(path='/items/{id:int}', methods=HTTPMethod.GET)
        async def handler(id: int): return ''

        router.validate()  # must not raise

    def test_router_frozen_after_validate(self):
        router = Router()

        @router.route(path='/x', methods=HTTPMethod.GET)
        async def handler(scope, receive, send): pass

        router.validate()
        assert router._frozen is True

    def test_annotation_mismatch_raises(self):
        router = Router()

        @router.route(path='/items/{id:int}', methods=HTTPMethod.GET)
        async def handler(id: str): return ''  # int converter but str annotation

        with pytest.raises(ConfigurationError, match="id"):
            router.validate()

    def test_bare_param_with_annotation_passes(self):
        # {id} (no explicit :converter) defaults to a 'str' router spec, but
        # _adapt_handler re-coerces the captured string to the handler's own
        # annotation at call time (docs/getting-started/first-app.md's
        # "str captured, int() applied" pattern) — not a converter/annotation
        # mismatch, unlike the explicit {id:int} case above.
        router = Router()

        @router.route(path='/items/{id}', methods=HTTPMethod.GET)
        async def handler(id: int): return ''

        router.validate()  # must not raise

    def test_validate_idempotent_after_error(self):
        router = Router()

        @router.route(path='/items/{id:int}', methods=HTTPMethod.GET)
        async def handler(id: str): return ''

        with pytest.raises(ConfigurationError):
            router.validate()
        # frozen must NOT be set when validation fails
        assert router._frozen is False


class TestFrozenRouter:
    def test_setitem_raises_after_freeze(self):
        router = Router()
        router.validate()  # freezes empty router

        async def fn(scope, receive, send): pass

        with pytest.raises(RuntimeError, match="frozen"):
            router[('/x', HTTPMethod.GET, Scheme.http)] = fn

    def test_route_raises_after_freeze(self):
        router = Router()
        router.validate()

        with pytest.raises(RuntimeError, match="frozen"):
            @router.route(path='/y', methods=HTTPMethod.GET)
            async def fn(scope, receive, send): pass


class TestCustomMethods:
    """RFC 9110 §9.1 — any token is a valid HTTP method; non-IANA methods
    (BREW, PROPFIND, WHEN, …) must be registerable and dispatchable."""

    def test_register_single_str_method(self, router):
        @router.route(path='/pot', methods='BREW')
        def fn(scope, receive, send): pass

        result = router[('/pot', 'BREW', Scheme.http)]
        assert result is fn

    def test_register_list_of_str_methods(self, router):
        @router.route(path='/pot', methods=['BREW', 'PROPFIND'])
        def fn(scope, receive, send): pass

        assert router[('/pot', 'BREW', Scheme.http)] is fn
        assert router[('/pot', 'PROPFIND', Scheme.http)] is fn

    def test_custom_method_dispatches_correctly(self, router):
        @router.route(path='/pot', methods='BREW')
        def brew_fn(scope, receive, send): pass

        result = router[('/pot', 'BREW', Scheme.http)]
        assert result is brew_fn

    def test_wrong_custom_method_raises_method_not_applicable(self, router):
        @router.route(path='/pot', methods='BREW')
        def fn(scope, receive, send): pass

        with pytest.raises(MethodNotApplicable) as exc_info:
            router[('/pot', 'FROBNICATE', Scheme.http)]
        assert 'BREW' in exc_info.value.allowed_methods

    def test_case_sensitivity_custom_method(self, router):
        @router.route(path='/pot', methods='BREW')
        def fn(scope, receive, send): pass

        with pytest.raises((MethodNotApplicable, PathNotRegistered)):
            router[('/pot', 'brew', Scheme.http)]

    def test_iana_and_custom_method_coexist(self, router):
        @router.route(path='/pot', methods=[HTTPMethod.GET, 'BREW'])
        def fn(scope, receive, send): pass

        assert router[('/pot', HTTPMethod.GET, Scheme.http)] is fn
        assert router[('/pot', 'BREW', Scheme.http)] is fn

    def test_custom_method_allow_header_populated(self, router):
        @router.route(path='/pot', methods='BREW')
        def fn(scope, receive, send): pass

        with pytest.raises(MethodNotApplicable) as exc_info:
            router[('/pot', 'WHEN', Scheme.http)]
        assert 'BREW' in exc_info.value.allowed_methods


class TestGetRoutes:
    """app.get_routes() / Router.get_routes() — public route introspection."""

    def test_empty_app_returns_empty_list(self):
        app = BlackBull()
        assert app.get_routes() == []

    def test_single_route(self):
        app = BlackBull()

        @app.route(path='/health')
        async def health():
            return 'ok'

        routes = app.get_routes()
        assert len(routes) == 1
        assert routes[0].method == 'GET'
        assert routes[0].path == '/health'
        assert routes[0].name == ''

    def test_multi_method_yields_one_entry_per_method(self):
        app = BlackBull()

        @app.route(path='/res', methods=[HTTPMethod.GET, HTTPMethod.POST])
        async def res():
            return 'ok'

        routes = app.get_routes()
        assert [(r.method, r.path) for r in routes] == [
            ('GET', '/res'), ('POST', '/res')]

    def test_path_params_preserved_as_template(self):
        app = BlackBull()

        @app.route(path='/tasks/{task_id:int}')
        async def get_task(task_id: int):
            return {'id': task_id}

        routes = app.get_routes()
        assert routes[0].path == '/tasks/{task_id:int}'

    def test_name_reported(self):
        app = BlackBull()

        @app.route(path='/named', name='my_route')
        async def named():
            return 'ok'

        assert app.get_routes()[0].name == 'my_route'

    def test_custom_method_reported_as_string(self):
        app = BlackBull()

        @app.route(path='/pot', methods='BREW')
        async def brew():
            return 'ok'

        assert app.get_routes()[0].method == 'BREW'

    def test_registration_order_preserved(self):
        app = BlackBull()

        @app.route(path='/a')
        async def a():
            return 'a'

        @app.route(path='/b')
        async def b():
            return 'b'

        assert [r.path for r in app.get_routes()] == ['/a', '/b']

    def test_returns_fresh_copy(self):
        app = BlackBull()

        @app.route(path='/x')
        async def x():
            return 'x'

        first = app.get_routes()
        first.clear()
        assert len(app.get_routes()) == 1

    def test_routeinfo_is_namedtuple(self):
        from blackbull import RouteInfo
        app = BlackBull()

        @app.route(path='/y')
        async def y():
            return 'y'

        r = app.get_routes()[0]
        assert isinstance(r, RouteInfo)
        method, path, name = r          # unpackable
        assert (method, path, name) == ('GET', '/y', '')
