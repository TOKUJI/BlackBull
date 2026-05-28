"""Unit tests for :class:`blackbull.server.deadline.ConnectionDeadline`.

The deadline replaces ``asyncio.timeout`` on the per-request hot path
(Sprint 23).  These tests pin down the behaviours the server stack
relies on:

* a fired deadline surfaces as ``TimeoutError`` via :meth:`guard`
* a deadline that does *not* fire returns the body result normally
* the timer is reusable across phases (arm → fire → arm → no-op)
* the same-tick race (read completes and timer fires in the same loop
  iteration) does not leave the task in a cancelled state
* ``seconds <= 0`` disables the deadline (legacy "timeout off" path)
* a non-deadline ``CancelledError`` propagates unchanged
"""
from __future__ import annotations

import asyncio

import pytest

from blackbull.server.deadline import ConnectionDeadline


@pytest.mark.asyncio
async def test_guard_returns_normally_when_under_deadline():
    dl = ConnectionDeadline()
    with dl.guard(0.5):
        await asyncio.sleep(0.01)
    assert not dl.fired


@pytest.mark.asyncio
async def test_guard_raises_timeout_when_deadline_fires():
    dl = ConnectionDeadline()
    with pytest.raises(TimeoutError):
        with dl.guard(0.01):
            await asyncio.sleep(1.0)
    assert dl.fired


@pytest.mark.asyncio
async def test_guard_zero_disables_deadline():
    dl = ConnectionDeadline()
    # 0 must NOT arm a timer — the call should pass through.  Use a
    # quick sleep so the test stays fast even if the contract regresses.
    with dl.guard(0.0):
        await asyncio.sleep(0.01)
    assert not dl.fired


@pytest.mark.asyncio
async def test_deadline_reusable_across_phases():
    """The same instance is rearmed for headers, then body chunks, then
    keep-alive idle.  After a phase completes, the next ``arm`` must
    start a fresh deadline with ``fired`` cleared."""
    dl = ConnectionDeadline()
    # Phase 1 — completes well under the deadline.
    with dl.guard(0.5):
        await asyncio.sleep(0.01)
    assert not dl.fired
    # Phase 2 — also completes; the timer from phase 1 must already be
    # cancelled so it can't bleed into phase 2.
    with dl.guard(0.5):
        await asyncio.sleep(0.01)
    assert not dl.fired


@pytest.mark.asyncio
async def test_deadline_reusable_after_firing():
    """Some call sites translate the TimeoutError into a 408 response
    and continue serving (the connection actor handles header-timeout
    this way before returning).  The deadline object itself must be
    safe to rearm after firing."""
    dl = ConnectionDeadline()
    with pytest.raises(TimeoutError):
        with dl.guard(0.01):
            await asyncio.sleep(1.0)
    assert dl.fired
    # Rearm — fired should reset and the next short await should pass.
    with dl.guard(0.5):
        await asyncio.sleep(0.01)
    assert not dl.fired


@pytest.mark.asyncio
async def test_unrelated_cancel_propagates():
    """If the task is cancelled by something other than the deadline
    (e.g. server shutdown), the guard must not swallow it."""
    dl = ConnectionDeadline()

    async def inner():
        with dl.guard(5.0):
            await asyncio.sleep(10.0)

    task = asyncio.create_task(inner())
    await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not dl.fired  # cancel came from outside the deadline


@pytest.mark.asyncio
async def test_guard_does_not_leak_cancel_after_timeout():
    """After ``guard`` raises ``TimeoutError`` on a fired deadline, the
    surrounding task must not be left in a cancelled state — the
    server's keep-alive loop continues to issue follow-up awaits (to
    send the 408 response, for example) and they must complete
    normally."""
    dl = ConnectionDeadline()
    with pytest.raises(TimeoutError):
        with dl.guard(0.01):
            await asyncio.sleep(1.0)
    # If ``guard`` didn't call ``task.uncancel()`` this sleep would
    # immediately raise CancelledError instead of completing.
    await asyncio.sleep(0.01)


@pytest.mark.asyncio
async def test_arm_cancels_previous_handle():
    """Calling ``arm`` again before the previous deadline fires must
    cancel the pending handle — otherwise the older, shorter deadline
    would fire mid-phase."""
    dl = ConnectionDeadline()
    dl.arm(0.01)   # would fire after 10 ms
    dl.arm(0.5)    # replaces the above
    # Wait past the original short deadline; should NOT fire.
    await asyncio.sleep(0.05)
    assert not dl.fired
    dl.disarm()
