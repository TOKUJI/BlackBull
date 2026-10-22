"""The one deadline primitive, enforced by the shared scanner.

``Deadline`` runs its callback once the deadline passes; ``Guard`` turns that
into ``TimeoutError`` for the body of a ``with`` block, the way the server
bounds a read or a drain.  Expiry may lag by one scanner tick.
"""
from __future__ import annotations

import asyncio
import subprocess
import sys

import pytest

from blackbull.server.deadline import TICK_S, Deadline, Guard, SharedGuard

pytestmark = pytest.mark.asyncio

PAST_A_TICK = TICK_S * 2 + 0.05


def _deadline():
    fired: list[None] = []
    return Deadline(lambda: fired.append(None)), fired


async def test_expiry_runs_the_callback_once():
    dl, fired = _deadline()
    dl.arm(0.01)
    await asyncio.sleep(PAST_A_TICK)
    await asyncio.sleep(PAST_A_TICK)
    assert fired == [None]


async def test_rearming_replaces_the_earlier_deadline():
    dl, fired = _deadline()
    dl.arm(0.01)
    dl.arm(PAST_A_TICK * 3)
    await asyncio.sleep(PAST_A_TICK)
    assert fired == []
    dl.disarm()


@pytest.mark.parametrize('stop', ['disarm', 'arm-zero'])
async def test_a_stopped_deadline_never_fires(stop):
    dl, fired = _deadline()
    dl.arm(0.01)
    dl.disarm() if stop == 'disarm' else dl.arm(0.0)
    await asyncio.sleep(PAST_A_TICK)
    assert fired == []


async def test_deadlines_are_independent():
    (a, fa), (b, fb) = _deadline(), _deadline()
    a.arm(0.01)
    b.arm(0.01)
    b.disarm()
    await asyncio.sleep(PAST_A_TICK)
    assert (fa, fb) == ([None], [])


async def test_a_deadline_armed_after_the_scanner_went_quiet_still_fires():
    dl, fired = _deadline()
    dl.arm(0.01)
    await asyncio.sleep(PAST_A_TICK)            # fires; the registry drains
    await asyncio.sleep(PAST_A_TICK)            # the scanner stops ticking
    dl.arm(0.01)
    await asyncio.sleep(PAST_A_TICK)
    assert fired == [None, None]


async def test_guard_raises_timeout_when_its_deadline_passes():
    guard = Guard()
    with pytest.raises(TimeoutError):
        with guard.within(0.01):
            await asyncio.sleep(10)


@pytest.mark.parametrize('seconds', [PAST_A_TICK * 3, 0.0],
                         ids=['under-deadline', 'zero-disables'])
async def test_guard_returns_normally_otherwise(seconds):
    with Guard().within(seconds) as bound:
        await asyncio.sleep(0.01)
    assert isinstance(bound, Guard)


async def test_a_guard_is_reusable_after_it_fires():
    guard = Guard()
    with pytest.raises(TimeoutError):
        with guard.within(0.01):
            await asyncio.sleep(10)
    with guard.within(PAST_A_TICK * 3):
        await asyncio.sleep(0.01)


async def test_after_a_timeout_the_task_is_not_left_cancelled():
    with pytest.raises(TimeoutError):
        with Guard().within(0.01):
            await asyncio.sleep(10)
    await asyncio.sleep(0.01)


async def test_an_unrelated_cancel_propagates():
    async def inner():
        with Guard().within(5.0):
            await asyncio.sleep(10)

    task = asyncio.create_task(inner())
    await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_an_exception_propagates_and_leaves_no_deadline_behind():
    with pytest.raises(ValueError, match='boom'):
        with Guard().within(0.01):
            raise ValueError('boom')
    await asyncio.sleep(PAST_A_TICK)            # a leftover would cancel this


async def test_a_task_riding_another_tasks_guard_is_not_cancelled():
    """Concurrent drains share one write deadline: only its owner is bound."""
    guard = SharedGuard(0.01)
    owner_entered = asyncio.Event()

    async def owner():
        with guard:
            owner_entered.set()
            await asyncio.sleep(10)

    async def rider():
        await owner_entered.wait()
        with guard:
            await asyncio.sleep(PAST_A_TICK)
        return 'finished'

    owning = asyncio.create_task(owner())
    riding = asyncio.create_task(rider())
    with pytest.raises(TimeoutError):
        await owning
    assert await riding == 'finished'


async def test_a_raising_callback_does_not_stop_the_scanner(caplog):
    """One bad callback must not end enforcement for every other deadline."""
    def explode() -> None:
        raise RuntimeError('boom')

    bad = Deadline(explode)
    good, fired = _deadline()
    bad.arm(0.01)
    await asyncio.sleep(PAST_A_TICK)
    good.arm(0.01)
    await asyncio.sleep(PAST_A_TICK)
    assert fired == [None]


async def test_a_guard_cancels_the_task_that_created_it():
    """A connection's read deadline belongs to the connection's task, whichever
    await inside it is running when the deadline passes."""
    async def connection():
        guard = Guard()
        with guard.within(0.01):
            await asyncio.sleep(10)

    with pytest.raises(TimeoutError):
        await asyncio.wait_for(connection(), 5)


def test_a_shared_guard_works_before_any_other_deadline_was_armed():
    """A client-only process drains through a writer before anything else arms."""
    code = ('import asyncio\n'
            'from blackbull.server.deadline import SharedGuard\n'
            'async def main():\n'
            '    with SharedGuard(1.0):\n'
            '        await asyncio.sleep(0)\n'
            'asyncio.run(main())\n')
    done = subprocess.run([sys.executable, '-c', code], capture_output=True,
                          text=True, timeout=30)
    assert done.returncode == 0, done.stderr
