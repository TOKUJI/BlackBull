"""Integration tests for BlackBull's event API (@app.on, @app.intercept).

These tests cover the BlackBull class's decorator API and verify that
registered handlers are actually invoked when events are emitted through
the dispatcher. Pure dispatcher behaviour is covered separately in
tests/test_event.py.
"""
import asyncio
import pytest

from blackbull import BlackBull, Event


@pytest.mark.asyncio
async def test_intercept_decorator_registers_handler():
    """@app.intercept registers a handler that fires on emit."""
    app = BlackBull()
    received = []

    @app.intercept('custom_event')
    async def handler(event: Event):
        received.append(event)

    await app._dispatcher.emit(Event('custom_event', {'k': 'v'}))

    assert len(received) == 1
    assert received[0].name == 'custom_event'
    assert received[0].detail == {'k': 'v'}


@pytest.mark.asyncio
async def test_on_decorator_registers_handler():
    """@app.on registers an observer that fires on emit."""
    app = BlackBull()
    received = asyncio.Event()
    captured = []

    @app.on('custom_event')
    async def handler(event: Event):
        captured.append(event)
        received.set()

    await app._dispatcher.emit(Event('custom_event', {'k': 'v'}))
    await asyncio.wait_for(received.wait(), timeout=1.0)

    assert len(captured) == 1
    assert captured[0].name == 'custom_event'


@pytest.mark.asyncio
async def test_intercept_decorator_returns_original_function():
    """@app.intercept returns the original function unchanged."""
    app = BlackBull()

    async def my_handler(event: Event):
        pass

    returned = app.intercept('custom_event')(my_handler)
    assert returned is my_handler


@pytest.mark.asyncio
async def test_on_decorator_returns_original_function():
    """@app.on returns the original function unchanged."""
    app = BlackBull()

    async def my_handler(event: Event):
        pass

    returned = app.on('custom_event')(my_handler)
    assert returned is my_handler


@pytest.mark.asyncio
async def test_multiple_intercept_handlers_run_in_registration_order():
    """Multiple interceptors registered via @app.intercept run in order."""
    app = BlackBull()
    order = []

    @app.intercept('custom_event')
    async def first(event: Event):
        order.append(1)

    @app.intercept('custom_event')
    async def second(event: Event):
        order.append(2)

    @app.intercept('custom_event')
    async def third(event: Event):
        order.append(3)

    await app._dispatcher.emit(Event('custom_event'))
    assert order == [1, 2, 3]


@pytest.mark.asyncio
async def test_multiple_on_handlers_all_fire():
    """Multiple observers registered via @app.on all run."""
    app = BlackBull()
    barrier = asyncio.Event()
    captured = set()

    @app.on('custom_event')
    async def first(event: Event):
        captured.add('first')
        if len(captured) == 3:
            barrier.set()

    @app.on('custom_event')
    async def second(event: Event):
        captured.add('second')
        if len(captured) == 3:
            barrier.set()

    @app.on('custom_event')
    async def third(event: Event):
        captured.add('third')
        if len(captured) == 3:
            barrier.set()

    await app._dispatcher.emit(Event('custom_event'))
    await asyncio.wait_for(barrier.wait(), timeout=1.0)

    assert captured == {'first', 'second', 'third'}


@pytest.mark.asyncio
async def test_intercept_and_on_can_coexist_for_same_event():
    """The same event can have both interceptors and observers registered."""
    app = BlackBull()
    intercepted = []
    observed = asyncio.Event()
    observed_event = []

    @app.intercept('custom_event')
    async def intercept_handler(event: Event):
        intercepted.append(event)

    @app.on('custom_event')
    async def observe_handler(event: Event):
        observed_event.append(event)
        observed.set()

    await app._dispatcher.emit(Event('custom_event', {'tag': 'both'}))
    await asyncio.wait_for(observed.wait(), timeout=1.0)

    assert len(intercepted) == 1
    assert len(observed_event) == 1
    assert intercepted[0].detail == {'tag': 'both'}
    assert observed_event[0].detail == {'tag': 'both'}


@pytest.mark.asyncio
async def test_intercept_handler_exception_propagates_through_app():
    """Interceptor exceptions reach the caller of emit, even via BlackBull."""
    app = BlackBull()

    @app.intercept('custom_event')
    async def boom(event: Event):
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError, match="boom"):
        await app._dispatcher.emit(Event('custom_event'))


@pytest.mark.asyncio
async def test_on_handler_exception_does_not_propagate():
    """Observer exceptions are isolated, even when registered via @app.on."""
    app = BlackBull()

    @app.on('custom_event')
    async def boom(event: Event):
        raise RuntimeError("boom")

    # Must not raise
    await app._dispatcher.emit(Event('custom_event'))
    # Give the observer task a chance to run and fail internally
    await asyncio.sleep(0)