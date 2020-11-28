import asyncio
import pytest
from BlackBull import EventEmitter
from BlackBull.logger import get_logger_set  # , ColoredFormatter

# Test targets
# from plugin import Plugin, LedgerInfo

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
