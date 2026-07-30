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
    await asyncio.wait_for(d.aclose(), timeout=1.0)


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
    """Completed observer tasks are removed — aclose() returns without hanging."""
    d = EventDispatcher()

    async def quick(event):
        pass

    d.on('test', quick)
    await d.emit(Event('test'))
    await asyncio.wait_for(d.aclose(), timeout=1.0)


# ---------------------------------------------------------------------------
# Blocking observers — the third delivery mode (awaited + isolated)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_blocking_observer_is_awaited_before_emit_returns():
    """Unlike a detached observer, a blocking one completes before emit() returns."""
    d = EventDispatcher()
    done = []

    async def cleanup(event):
        await asyncio.sleep(0)  # yield, then finish
        done.append('done')

    d.on('test', cleanup, blocking=True)
    await d.emit(Event('test'))
    # No sleep / no aclose needed — it already ran to completion inside emit.
    assert done == ['done']


@pytest.mark.asyncio
async def test_blocking_observers_run_in_registration_order():
    d = EventDispatcher()
    order = []

    async def first(event):
        await asyncio.sleep(0)
        order.append('first')

    async def second(event):
        order.append('second')

    d.on('test', first, blocking=True)
    d.on('test', second, blocking=True)
    await d.emit(Event('test'))
    assert order == ['first', 'second']


@pytest.mark.asyncio
async def test_blocking_observer_exception_is_isolated():
    """A blocking observer's exception is logged, not propagated, and does not
    abort sibling blocking observers."""
    d = EventDispatcher()
    ran = []

    async def boom(event):
        raise RuntimeError('cleanup failed')

    async def survivor(event):
        ran.append('survivor')

    d.on('test', boom, blocking=True)
    d.on('test', survivor, blocking=True)
    await d.emit(Event('test'))  # must NOT raise
    assert ran == ['survivor']


@pytest.mark.asyncio
async def test_emit_order_interceptors_then_blocking_then_detached():
    d = EventDispatcher()
    order = []

    async def interceptor(event):
        order.append('intercept')

    async def blocking(event):
        order.append('blocking')

    async def detached(event):
        order.append('detached')

    d.intercept('test', interceptor)
    d.on('test', detached)               # detached
    d.on('test', blocking, blocking=True)
    await d.emit(Event('test'))
    # Interceptor and blocking observer have run synchronously and in that
    # order; the detached observer has not necessarily run yet.
    assert order[:2] == ['intercept', 'blocking']
    await asyncio.wait_for(d.aclose(), timeout=1.0)
    assert 'detached' in order


@pytest.mark.asyncio
async def test_blocking_observer_counts_as_a_listener():
    d = EventDispatcher()

    async def cleanup(event):
        pass

    assert d.has_listeners('test') is False
    d.on('test', cleanup, blocking=True)
    assert d.has_listeners('test') is True

# ---------------------------------------------------------------------------
# has_listeners — the per-request guard on every lifecycle emit site
# ---------------------------------------------------------------------------

def _has_listeners_reference(d: EventDispatcher, name: str) -> bool:
    """The predicate as it stood before the registration index."""
    return (bool(d._interceptors.get(name))
            or bool(d._blocking_observers.get(name))
            or bool(d._observers.get(name)))


@pytest.mark.parametrize('kind', ['intercept', 'observe', 'blocking'])
def test_has_listeners_agrees_with_the_three_dict_predicate(kind):
    """The index must answer exactly what scanning the three dicts answers."""
    d = EventDispatcher()

    async def handler(event):
        pass

    assert d.has_listeners('e') is _has_listeners_reference(d, 'e')
    if kind == 'intercept':
        d.intercept('e', handler)
    elif kind == 'observe':
        d.on('e', handler)
    else:
        d.on('e', handler, blocking=True)
    assert d.has_listeners('e') is _has_listeners_reference(d, 'e') is True
    # A name nobody registered stays False.
    assert d.has_listeners('other') is _has_listeners_reference(d, 'other')


def test_has_listeners_does_not_create_empty_entries():
    """Asking about an unknown event must not grow the defaultdicts."""
    d = EventDispatcher()
    for _ in range(3):
        assert d.has_listeners('never-registered') is False
    assert 'never-registered' not in d._observers
    assert 'never-registered' not in d._interceptors
    assert 'never-registered' not in d._blocking_observers


def test_registration_bumps_generation_so_cached_guards_invalidate():
    """EventAggregator caches derived booleans against this counter."""
    d = EventDispatcher()

    async def handler(event):
        pass

    before = d.generation
    d.on('e', handler)
    assert d.generation > before
    mid = d.generation
    d.intercept('e', handler)
    assert d.generation > mid
