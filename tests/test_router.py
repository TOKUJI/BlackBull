from logging import getLogger
# import asyncio
import pytest

# Test targets
from blackbull.utils import Scheme, HTTPMethods
from blackbull.router import Router

logger = getLogger(__name__)


@pytest.fixture
def router():
    router = Router()
    yield router


def test_router_add(router):
    path = 'test'
    scheme = Scheme.http
    key = (path, scheme)

    logger.info('Before registration.')

    @router.route(path=path, scheme=scheme)
    def test_fn(*args, **kwargs):
        pass

    logger.info('Registered.')
    f, m = router[key]

    assert f == test_fn
    assert m == [HTTPMethods.get]


@pytest.mark.asyncio
async def test_router_add_middleware_stack1(router):
    path = 'test'
    scheme = Scheme.http
    key = (path, scheme)

    async def test_fn1(scope, receive, send, inner):
        logger.info('test_fn1 starts.')
        await inner(scope, receive, send)
        logger.info('test_fn1 ends.')
        return 'fn1'

    router.route(methods=[HTTPMethods.get], path=path, functions=[test_fn1])

    logger.info('Registered.')
    fn, ms = router[key]
    assert ms == [HTTPMethods.get]
    logger.info(fn)

    res = await fn({}, None, None)
    assert res == 'fn1'


@pytest.mark.asyncio
async def test_router_add_middleware_stack2(router):
    path = 'test'
    scheme = Scheme.http
    key = (path, scheme)

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

    router.route(methods=[HTTPMethods.get], path=path, functions=[test_fn1, test_fn2, test_fn3])

    logger.info('Registered.')
    fn, ms = router[key]

    assert ms == [HTTPMethods.get]
    logger.info(fn)

    res = await fn({}, None, None)
    assert res == 'fn3fn2fn1'


def test_router_add_post(router):
    path = 'test'
    scheme = Scheme.http
    key = (path, scheme)

    @router.route(path=path, methods=[HTTPMethods.post])
    def fn(*args, **kwargs):
        pass

    f, m = router[key]

    assert f == fn
    assert m == [HTTPMethods.post]


def test_router_regex(router):
    path = r'^test/\d+$'
    scheme = Scheme.http
    key = (path, scheme)

    @router.route(path=path, methods=[HTTPMethods.get])
    def fn(*args, **kwargs):
        pass

    f, m = router[('test/1234', scheme)]

    assert f == fn
    assert m == [HTTPMethods.get]


def test_router_regex_with_group_name1(router):
    path = r'^test/(?P<id_>\d+)$'
    scheme = Scheme.http
    key = (path, scheme)

    @router.route(path=path, methods=[HTTPMethods.get])
    def fn(*args, **kwargs):
        return kwargs.pop('id_', None)

    f, m = router[('test/1234', scheme)]

    assert m == [HTTPMethods.get]
    assert f() == '1234'


def test_router_regex_with_group_name2(router):
    path = r'^test/(?P<id_>\d+)$'
    scheme = Scheme.http

    @router.route(path=path, methods=[HTTPMethods.get])
    def fn(id_, *args, **kwargs):
        return id_

    f, m = router[('test/1234', scheme)]

    assert m == [HTTPMethods.get]
    assert f() == '1234'


def test_router_F_string1(router):
    path = 'test/{id_}'
    scheme = Scheme.http

    @router.route(path=path, methods=[HTTPMethods.get])
    def fn(*args, **kwargs):
        return kwargs.pop('id_', None)

    id_ = 'a24_12-3.4~'
    f, m = router[(f'test/{id_}', scheme)]

    assert m == [HTTPMethods.get]
    assert f() == id_


def test_router_F_string2(router):
    path = 'test/{id_}/{name}'
    scheme = Scheme.http

    @router.route(path=path, methods=[HTTPMethods.get])
    def fn(id_, name, *args, **kwargs):
        return (id_, name)

    id_ = 'a24_12-3.4~'
    name = 'Toshio'
    f, m = router[(f'test/{id_}/{name}', scheme)]

    assert m == [HTTPMethods.get]
    assert f() == (id_, name)


def test_router_websocket(router):
    path = 'test/{id_}'
    scheme = Scheme.websocket

    @router.route(path=path, scheme=scheme)
    def fn(*args, **kwargs):
        return kwargs.pop('id_', None)

    logger.debug('Registered')
    id_ = 'a24_12-3.4~'
    f, m = router[(f'test/{id_}', scheme)]

    assert m == [HTTPMethods.get]
    assert f() == id_
