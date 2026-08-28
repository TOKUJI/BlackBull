"""A stream that never starts must not strand its coroutine.

Ten of these on a sixteen-profile EC2 run, all HTTP/2.  Three local attempts
missed it because none had tasks *queued on the cap* when the cancellation
arrived — which is the whole condition.
"""
from __future__ import annotations

import asyncio
import gc
import warnings

import pytest

from blackbull.server.http2_actor import _run_when_stream_cap_admits

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


async def test_tasks_cancelled_while_queued_strand_nothing():
    """More streams than the cap, and a connection that goes away."""
    ran: list[str] = []

    async def handler():
        ran.append('ran')

    sem = asyncio.Semaphore(1)
    with _OrphanWatch() as watch:
        async with sem:                       # every task below must queue
            tasks = [asyncio.create_task(_run_when_stream_cap_admits(handler, sem))
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
    """Control: without it, "nothing stranded" passes by never running."""
    ran: list[str] = []

    async def handler():
        ran.append('ran')

    sem = asyncio.Semaphore(2)
    with _OrphanWatch() as watch:
        await asyncio.gather(*(_run_when_stream_cap_admits(handler, sem) for _ in range(6)))

    assert ran == ['ran'] * 6
    assert not watch.found, watch.found


async def test_a_factory_is_what_the_guard_takes():
    """Passing a coroutine is the bug; the signature is what stops it coming
    back."""
    calls: list[int] = []

    async def handler():
        return None

    def factory():
        calls.append(1)
        return handler()

    sem = asyncio.Semaphore(1)
    async with sem:
        task = asyncio.create_task(_run_when_stream_cap_admits(factory, sem))
        await asyncio.sleep(0.05)
        assert calls == [], 'the factory must not run before the semaphore admits'
        task.cancel()
        with _OrphanWatch() as watch:
            await asyncio.gather(task, return_exceptions=True)
            gc.collect()
    assert not watch.found, watch.found
