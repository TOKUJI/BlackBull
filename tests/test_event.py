"""Tests for blackbull.event — EventDispatcher and Event dataclass."""
import asyncio
import pytest
from blackbull.event import Event, EventDispatcher


@pytest.mark.asyncio
async def test_intercept_handler_runs_on_emit():
    d = EventDispatcher()
    called = []

    async def handler(event):
        called.append(event)

    d.intercept('test', handler)
    await d.emit(Event('test', {'k': 'v'}))
    assert len(called) == 1
    assert called[0].name == 'test'
    assert called[0].detail == {'k': 'v'}


@pytest.mark.asyncio
async def test_observe_handler_runs_on_emit():
    d = EventDispatcher()
    called = asyncio.Event()

    async def handler(event):
        called.set()

    d.on('test', handler)
    await d.emit(Event('test'))
    await asyncio.wait_for(called.wait(), timeout=1.0)


@pytest.mark.asyncio
async def test_interceptors_run_in_registration_order():
    d = EventDispatcher()
    order = []

    async def first(event): order.append(1)
    async def second(event): order.append(2)
    async def third(event): order.append(3)

    d.intercept('test', first)
    d.intercept('test', second)
    d.intercept('test', third)
    await d.emit(Event('test'))
    assert order == [1, 2, 3]


@pytest.mark.asyncio
async def test_interceptor_exception_propagates_to_emitter():
    d = EventDispatcher()

    async def boom(event):
        raise RuntimeError("boom")

    d.intercept('test', boom)
    with pytest.raises(RuntimeError, match="boom"):
        await d.emit(Event('test'))


@pytest.mark.asyncio
async def test_interceptor_exception_skips_subsequent_interceptors():
    d = EventDispatcher()
    after = []

    async def boom(event):
        raise RuntimeError("boom")

    async def later(event):
        after.append(True)

    d.intercept('test', boom)
    d.intercept('test', later)
    with pytest.raises(RuntimeError):
        await d.emit(Event('test'))
    assert after == []


@pytest.mark.asyncio
async def test_observer_exception_does_not_propagate():
    d = EventDispatcher()

    async def boom(event):
        raise RuntimeError("boom")

    d.on('test', boom)
    await d.emit(Event('test'))
    await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_observer_exception_does_not_affect_other_observers():
    d = EventDispatcher()
    survived = asyncio.Event()

    async def boom(event):
        raise RuntimeError("boom")

    async def survivor(event):
        survived.set()

    d.on('test', boom)
    d.on('test', survivor)
    await d.emit(Event('test'))
    await asyncio.wait_for(survived.wait(), timeout=1.0)


@pytest.mark.asyncio
async def test_emit_with_no_handlers_is_a_noop():
    d = EventDispatcher()
    await d.emit(Event('nobody_listening'))


@pytest.mark.asyncio
async def test_event_detail_defaults_to_empty_dict():
    e = Event('x')
    assert e.detail == {}


def test_event_is_frozen():
    e = Event('x', {'k': 1})
    with pytest.raises(Exception):
        e.name = 'y'  # type: ignore[misc]

@pytest.mark.asyncio
async def test_aclose_waits_for_pending_observers():
    """aclose() waits for in-flight observer tasks to finish."""
    d = EventDispatcher(shutdown_timeout=1.0)
    finished = []

    async def slow_observer(event):
        await asyncio.sleep(0.05)
        finished.append(event.name)

    d.on('test', slow_observer)
    await d.emit(Event('test'))
    await d.aclose()
    assert finished == ['test']


@pytest.mark.asyncio
async def test_aclose_cancels_observers_exceeding_timeout(caplog):
    """Observers that exceed the timeout are cancelled and logged."""
    import logging
    d = EventDispatcher(shutdown_timeout=0.05)

    async def hung_observer(event):
        await asyncio.sleep(10)

    d.on('test', hung_observer)
    await d.emit(Event('test'))

    with caplog.at_level(logging.WARNING):
        await d.aclose()

    assert any(
        "did not finish" in record.message
        for record in caplog.records
    )


@pytest.mark.asyncio
async def test_aclose_with_no_pending_tasks_is_a_noop():
    """aclose() returns immediately when nothing is pending."""
    d = EventDispatcher()
    await d.aclose()


@pytest.mark.asyncio
async def test_pending_tasks_are_removed_when_completed():
    """Completed observer tasks are removed from the pending set."""
    d = EventDispatcher()

    async def quick(event):
        pass

    d.on('test', quick)
    await d.emit(Event('test'))
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert len(d._pending_tasks) == 0