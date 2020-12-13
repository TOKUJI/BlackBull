import asyncio
import pytest
from BlackBull.logger import get_logger_set  # , ColoredFormatter

# Test targets
from BlackBull import EventEmitter
from BlackBull.utils import Router
from BlackBull.response import respond

logger, _ = get_logger_set('test_util')


# @pytest.mark.asyncio
# async def test_EventEmitter_on():

#     called = False

#     def is_a_function():
#         nonlocal called
#         called = True

#     is_a_function()

#     assert called is True, "is_a_function has not called."

#     called = False

#     assert called is False, "Failed to reset a variable."

#     event1 = asyncio.Event()
#     event2 = asyncio.Event()

#     emitter = EventEmitter()

#     emitter.on(event1, is_a_function)
#     assert called is False, "The listener is called before the emition of the event."

#     emitter.emit(event2)
#     assert called is False, "The listener is called before the emition of the event."

#     emitter.emit(event1)
#     await asyncio.sleep(0)  # Waits to run a function
#     assert called is True, "The listener is not called after the emition of the event."


# @pytest.mark.asyncio
# async def test_EventEmitter_twice():

#     count = 0

#     def add1():
#         nonlocal count
#         count = count + 1

#     event = asyncio.Event()

#     emitter = EventEmitter()

#     emitter.on(event, add1)
#     emitter.on(event, add1)

#     emitter.emit(event)
#     await asyncio.sleep(0)  # Waits to run a function
#     assert count == 2, "The listener is not called after the emition of the event."


# @pytest.mark.asyncio
# async def test_EventEmitter_parameter():

#     count = 0

#     def add1(n):
#         nonlocal count
#         count = count + n

#     event = asyncio.Event()

#     emitter = EventEmitter()

#     emitter.on(event, add1)

#     emitter.emit(event, 2)
#     await asyncio.sleep(0)  # Waits to run a function
#     assert count == 2, "The listener is not called after the emition of the event."


# @pytest.mark.asyncio
# async def test_EventEmitter_off():

#     count = 0

#     def add1():
#         nonlocal count
#         count = count + 1

#     event = asyncio.Event()

#     emitter = EventEmitter()

#     emitter.on(event, add1)
#     emitter.on(event, add1)
#     emitter.off(event, add1)

#     emitter.emit(event)
#     await asyncio.sleep(0)  # Waits to run a function
#     assert count == 1, "The listener is not called after the emition of the event."


@pytest.fixture
def router():
    router = Router()
    yield router


def test_router_add(router):
    key = 'test'

    logger.info('Before registration.')

    @router.route(path=key)
    def test_fn(*args, **kwargs):
        pass

    logger.info('Registered.')
    f, m = router[key]

    assert f == test_fn
    assert m == ['get']


@pytest.mark.asyncio
async def test_router_add_middleware_stack1(router):
    key = 'test'

    async def test_fn1(scope, receive, send, next_):
        logger.info('test_fn1 starts.')
        await next_(scope, receive, send)
        logger.info('test_fn1 ends.')
        return 'fn1'

    router.route(methods='get', path=key, functions=[test_fn1])

    logger.info('Registered.')
    fn, ms = router[key]
    assert ms == ['get']
    logger.info(fn)

    res = await fn({}, None, None)
    assert res == 'fn1'


@pytest.mark.asyncio
async def test_router_add_middleware_stack2(router):
    key = 'test'

    async def test_fn1(scope, receive, send, next_):
        logger.info('test_fn1 starts.')
        res = await next_(scope, receive, send)
        logger.info('test_fn1 ends.')
        return res + 'fn1'

    async def test_fn2(scope, receive, send, next_):
        logger.info('test_fn2 starts.')
        res = await next_(scope, receive, send)
        logger.info('test_fn2 ends.')
        return res + 'fn2'

    async def test_fn3(scope, receive, send, next_):
        logger.info('test_fn3 starts.')
        await next_(scope, receive, send)
        logger.info('test_fn3 ends.')
        return 'fn3'

    router.route(methods='get', path=key, functions=[test_fn1, test_fn2, test_fn3])

    logger.info('Registered.')
    fn, ms = router[key]

    assert ms == ['get']
    logger.info(fn)

    res = await fn({}, None, None)
    assert res == 'fn3fn2fn1'


def test_router_add_post(router):
    key = 'test'

    @router.route(path=key, methods='post')
    def fn(*args, **kwargs):
        pass

    f, m = router[key]

    assert f == fn
    assert m == ['post']


def test_router_regex(router):
    key = r'^test/\d+$'

    @router.route(path=key, methods='get')
    def fn(*args, **kwargs):
        pass

    f, m = router['test/1234']

    assert f == fn
    assert m == ['get']


def test_router_regex_with_group_name1(router):
    key = r'^test/(?P<id_>\d+)$'

    @router.route(path=key, methods='get')
    def fn(*args, **kwargs):
        return kwargs.pop('id_', None)

    f, m = router['test/1234']

    assert m == ['get']
    assert f() == '1234'


def test_router_regex_with_group_name2(router):
    key = r'^test/(?P<id_>\d+)$'

    @router.route(path=key, methods='get')
    def fn(id_, *args, **kwargs):
        return id_

    f, m = router['test/1234']

    assert m == ['get']
    assert f() == '1234'


def test_router_F_string(router):
    key = 'test/{id_}'

    @router.route(path=key, methods='get')
    def fn(*args, **kwargs):
        return kwargs.pop('id_', None)

    id_ = 'a24_12-3.4~'
    f, m = router[f'test/{id_}']

    assert m == ['get']
    assert f() == id_
