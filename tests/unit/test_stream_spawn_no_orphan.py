"""A stream that never starts must not strand its coroutine.

`http2_actor.py` builds `StreamActor(...).run()` and hands it to a task.
Between building and running there are two places the coroutine may never
reach: the semaphore in `_run_guarded`, where a cancelled task never enters
the body, and `create_task` itself when the connection's TaskGroup is already
winding down.  A coroutine built before either destroys un-awaited, and Python
reports it whenever the GC gets there — naming a stream unrelated to wherever
the line lands.

Measured before the fix, on a sixteen-profile EC2 run: ten of these, all on
HTTP/2 lanes.  Three local reproduction attempts missed it (synthetic aborts,
h2load torn down mid-flight, multiplexed streams) because none of them had
tasks *queued on the semaphore* when the cancellation arrived: it takes more
concurrent streams than `h2_active_streams` allows, plus a teardown.
"""
from __future__ import annotations

import asyncio
import gc
import warnings

import pytest

from blackbull.server.http2_actor import _run_guarded

pytestmark = pytest.mark.asyncio


class _OrphanWatch:
    """Collect 'was never awaited' warnings, including ones the GC raises."""

    def __enter__(self):
        self.found: list[str] = []
        self._original = warnings.showwarning

        def hook(message, category, filename, lineno, file=None, line=None):
            if category is RuntimeWarning and 'never awaited' in str(message):
                self.found.append(str(message))

        warnings.showwarning = hook
        warnings.simplefilter('always')
        return self

    def __exit__(self, *exc):
        gc.collect()
        warnings.showwarning = self._original
        return False


async def test_tasks_cancelled_while_queued_on_the_semaphore_strand_nothing():
    """The shape the EC2 run was producing: more streams than the cap, and a
    connection that goes away while they wait."""
    ran: list[str] = []

    async def handler():
        ran.append('ran')

    sem = asyncio.Semaphore(1)
    with _OrphanWatch() as watch:
        async with sem:                       # every task below must queue
            tasks = [asyncio.create_task(_run_guarded(handler, sem))
                     for _ in range(20)]
            await asyncio.sleep(0.05)
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        del tasks
        gc.collect()
        await asyncio.sleep(0)
        gc.collect()

    assert not watch.found, watch.found
    assert ran == [], 'no queued handler should have run'


async def test_the_semaphore_still_admits_and_runs():
    """Control: the cap is a cap, not a block.  Without this, "nothing was
    stranded" would pass by never running anything."""
    ran: list[str] = []

    async def handler():
        ran.append('ran')

    sem = asyncio.Semaphore(2)
    with _OrphanWatch() as watch:
        await asyncio.gather(*(_run_guarded(handler, sem) for _ in range(6)))

    assert ran == ['ran'] * 6
    assert not watch.found, watch.found


async def test_a_factory_is_what_the_guard_takes():
    """The contract, stated as a test: passing a coroutine is the bug.

    A ready-made coroutine survives only if the task reaches the body — which
    is exactly what cancellation prevents.  Pinning the signature keeps a
    future edit from quietly reverting to the shape that stranded 19 of 20.
    """
    calls: list[int] = []

    async def handler():
        return None

    def factory():
        calls.append(1)
        return handler()

    sem = asyncio.Semaphore(1)
    async with sem:
        task = asyncio.create_task(_run_guarded(factory, sem))
        await asyncio.sleep(0.05)
        assert calls == [], 'the factory must not run before the semaphore admits'
        task.cancel()
        with _OrphanWatch() as watch:
            await asyncio.gather(task, return_exceptions=True)
            gc.collect()
    assert not watch.found, watch.found
