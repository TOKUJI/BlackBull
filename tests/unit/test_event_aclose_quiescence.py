"""Shutdown drains observers to quiescence, not to one snapshot.

`aclose` used to wait on ``list(self._pending_tasks)`` taken once.  An
observer may itself ``emit``, so the second observer's task is created while
that wait is already running and is therefore not in the list being awaited.
The wait returned when the first generation finished and reported a clean
drain with the second still going — and silently, because the overrun WARNING
only covers tasks that were *in* the snapshot.

`docs/guide/events.md` presents the fan-out shape (close the session, then
emit an audit record) as the documented use of a terminal event, so this is
the ordinary case rather than an exotic one.
"""
from __future__ import annotations

import asyncio
import logging

import pytest

from blackbull.event import Event, EventDispatcher

pytestmark = pytest.mark.asyncio


async def test_an_observer_that_emits_is_drained_too():
    """Both generations must have run before ``aclose`` returns."""
    ran: list[str] = []
    dispatcher = EventDispatcher(shutdown_timeout=5.0)

    async def second(event):
        await asyncio.sleep(0.05)
        ran.append('second')

    async def first(event):
        await asyncio.sleep(0.05)
        ran.append('first')
        await dispatcher.emit(Event('chained', {}))

    dispatcher.on('start', first)
    dispatcher.on('chained', second)

    await dispatcher.emit(Event('start', {}))
    await dispatcher.aclose()

    assert ran == ['first', 'second'], ran


async def test_a_single_observer_still_drains():
    """Control: the shape that always worked must keep working."""
    ran: list[str] = []
    dispatcher = EventDispatcher(shutdown_timeout=5.0)

    async def solo(event):
        await asyncio.sleep(0.05)
        ran.append('solo')

    dispatcher.on('start', solo)
    await dispatcher.emit(Event('start', {}))
    await dispatcher.aclose()

    assert ran == ['solo']


async def test_an_overrunning_observer_is_still_warned_about_and_cancelled(caplog):
    """The cancel-on-overrun contract is unchanged: a shutdown that must
    finish still finishes."""
    started = asyncio.Event()
    finished: list[str] = []
    dispatcher = EventDispatcher(shutdown_timeout=0.05)

    async def slow(event):
        started.set()
        await asyncio.sleep(10)
        finished.append('slow')

    dispatcher.on('start', slow)
    await dispatcher.emit(Event('start', {}))
    await started.wait()

    with caplog.at_level(logging.WARNING):
        await dispatcher.aclose()

    assert not finished, 'the overrunning observer should have been cancelled'
    assert any('did not finish' in r.message or 'did not finish' in r.getMessage()
               for r in caplog.records), [r.getMessage() for r in caplog.records]


async def test_a_never_quiescing_chain_is_bounded_by_the_timeout():
    """An observer chain that emits on every hop cannot hold shutdown open
    past ``shutdown_timeout`` — the risk the proposal called out is a latency
    risk, not an unbounded one."""
    dispatcher = EventDispatcher(shutdown_timeout=0.2)

    async def forever(event):
        await asyncio.sleep(0.01)
        await dispatcher.emit(Event('loop', {}))

    dispatcher.on('loop', forever)
    await dispatcher.emit(Event('loop', {}))

    loop = asyncio.get_running_loop()
    began = loop.time()
    await asyncio.wait_for(dispatcher.aclose(), timeout=3.0)
    elapsed = loop.time() - began

    assert elapsed < 1.5, f'aclose took {elapsed:.2f}s against a 0.2s budget'
