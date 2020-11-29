import asyncio
import pytest
from BlackBull.logger import get_logger_set  # , ColoredFormatter

# Test targets
from BlackBull import EventEmitter
from BlackBull.utils import Router

logger, _ = get_logger_set('test_util')


@pytest.mark.asyncio
async def test_EventEmitter_on():

    called = False

    def is_a_function():
        nonlocal called
        called = True

    is_a_function()

    assert called is True, "is_a_function has not called."

    called = False

    assert called is False, "Failed to reset a variable."

    event1 = asyncio.Event()
    event2 = asyncio.Event()

    emitter = EventEmitter()

    emitter.on(event1, is_a_function)
    assert called is False, "The listener is called before the emition of the event."

    emitter.emit(event2)
    assert called is False, "The listener is called before the emition of the event."

    emitter.emit(event1)
    await asyncio.sleep(1)  # Waits to run a function
    assert called is True, "The listener is not called after the emition of the event."


@pytest.mark.asyncio
async def test_EventEmitter_twice():

    count = 0

    def add1():
        nonlocal count
        count = count + 1

    event = asyncio.Event()

    emitter = EventEmitter()

    emitter.on(event, add1)
    emitter.on(event, add1)

    emitter.emit(event)
    await asyncio.sleep(1)  # Waits to run a function
    assert count == 2, "The listener is not called after the emition of the event."


@pytest.mark.asyncio
async def test_EventEmitter_parameter():

    count = 0

    def add1(n):
        nonlocal count
        count = count + n

    event = asyncio.Event()

    emitter = EventEmitter()

    emitter.on(event, add1)

    emitter.emit(event, 2)
    await asyncio.sleep(1)  # Waits to run a function
    assert count == 2, "The listener is not called after the emition of the event."


@pytest.mark.asyncio
async def test_EventEmitter_off():

    count = 0

    def add1():
        nonlocal count
        count = count + 1

    event = asyncio.Event()

    emitter = EventEmitter()

    emitter.on(event, add1)
    emitter.on(event, add1)
    emitter.off(event, add1)

    emitter.emit(event)
    await asyncio.sleep(1)  # Waits to run a function
    assert count == 1, "The listener is not called after the emition of the event."


@pytest.fixture
def router():
    router = Router()
    yield router


def test_router_add(router):
    key = 'test'

    @router.route(path=key)
    def fn(*args, **kwargs):
        pass

    f, m = router[key]

    assert f == fn
    assert m == ['GET']


def test_router_add_post(router):
    key = 'test'

    @router.route(path=key, methods='post')
    def fn(*args, **kwargs):
        pass

    f, m = router[key]

    assert f == fn
    assert m == ['POST']


def test_router_regex(router):
    key = r'^test/\d+$'

    @router.route(path=key, methods='get')
    def fn(*args, **kwargs):
        pass

    f, m = router['test/1234']

    assert f == fn
    assert m == ['GET']


def test_router_regex_with_group_name1(router):
    key = r'^test/(?P<id_>\d+)$'

    @router.route(path=key, methods='get')
    def fn(*args, **kwargs):
        return kwargs.pop('id_', None)

    f, m = router['test/1234']

    assert m == ['GET']
    assert f() == '1234'


def test_router_regex_with_group_name2(router):
    key = r'^test/(?P<id_>\d+)$'

    @router.route(path=key, methods='get')
    def fn(id_, *args, **kwargs):
        return id_

    f, m = router['test/1234']

    assert m == ['GET']
    assert f() == '1234'


# def test_router_F_string(router):
#     key = 'test/{id_}'

#     @router.route(path=key, methods='get')
#     def fn(*args, **kwargs):
#         return kwargs.pop('id_', None)

#     f, m = router['test/1234']

#     assert f == fn
#     assert m == ['GET']
#     assert f() == '1234'
