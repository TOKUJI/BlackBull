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

from blackbull.server.deadline import ConnectionDeadline, _Scanner


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
async def test_guard_propagates_non_cancel_exception():
    """If the body raises a non-CancelledError exception, ``guard`` must
    propagate it unchanged and still disarm the pending timer (Sprint 26
    Phase A — parity case 2 from the contextmanager-era implementation)."""
    dl = ConnectionDeadline()
    with pytest.raises(ValueError, match='boom'):
        with dl.guard(0.5):
            raise ValueError('boom')
    assert not dl.fired
    # Deadline must be disarmed even on raise — rearm should start clean.
    assert dl._deadline_at == float('inf')
    assert not dl._registered


@pytest.mark.asyncio
async def test_guard_same_tick_race_raises_timeout():
    """Same-tick race: the body completes normally but ``_fired`` is set
    in the same loop iteration (Sprint 26 Phase A — parity case 5).
    This is the convention ``asyncio.timeout`` uses: a deadline that
    fired wins even if the awaited read also produced a value."""
    dl = ConnectionDeadline()
    with pytest.raises(TimeoutError):
        with dl.guard(0.5):
            # Simulate the same-tick race by setting _fired without
            # going through _fire (which would also cancel the task).
            dl._fired = True
    # Disarmed and clean.
    assert dl._deadline_at == float('inf')
    assert not dl._registered


@pytest.mark.asyncio
async def test_guard_returns_self_as_context_manager():
    """``dl.guard(s)`` returns the deadline itself so ``with ... as x``
    binds ``x`` to ``dl``.  Locks in the protocol; calling code does not
    use the ``as`` form today but the parity case from the old
    @contextmanager form returned ``None``, so this is a deliberate
    extension."""
    dl = ConnectionDeadline()
    with dl.guard(0.5) as bound:
        assert bound is dl


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


# --- Scanner-architecture-specific tests (Sprint 26 Phase B) ---


@pytest.mark.asyncio
async def test_scanner_singleton_resurrects_after_quiesce():
    """The per-process scanner cancels its tick handle once the registry
    drains; the next ``arm`` must re-arm the scanner."""
    dl = ConnectionDeadline()
    dl.arm(5.0)
    assert _Scanner._HANDLE is not None
    assert dl in _Scanner._REGISTRY
    dl.disarm()
    assert dl not in _Scanner._REGISTRY
    # After disarm, registry may still hold the handle until the next
    # tick — but a fresh arm must observe the scanner running.  We can't
    # easily wait a full tick here, so the invariant we lock in is just
    # that re-arming re-registers.
    dl.arm(5.0)
    assert dl in _Scanner._REGISTRY
    assert _Scanner._HANDLE is not None
    dl.disarm()


@pytest.mark.asyncio
async def test_scanner_handles_multiple_independent_deadlines():
    """One scanner serves many deadlines.  Firing one must not affect
    the others; the registry must track each independently."""
    a, b, c = ConnectionDeadline(), ConnectionDeadline(), ConnectionDeadline()
    a.arm(5.0)
    b.arm(5.0)
    c.arm(5.0)
    assert {a, b, c} <= _Scanner._REGISTRY
    b.disarm()
    assert b not in _Scanner._REGISTRY
    assert a in _Scanner._REGISTRY
    assert c in _Scanner._REGISTRY
    a.disarm()
    c.disarm()


@pytest.mark.asyncio
async def test_scanner_fires_short_deadline_within_tick_window():
    """End-to-end: a deadline armed past ``_TICK_S`` must fire through
    the scanner.  Uses guard() which translates the cancellation into
    ``TimeoutError`` — same observable shape as Sprint 23."""
    dl = ConnectionDeadline()
    with pytest.raises(TimeoutError):
        with dl.guard(0.05):
            # Sleep long enough that the scanner is guaranteed to
            # observe the expiry (one full _TICK_S past the deadline).
            await asyncio.sleep(1.0)
    assert dl.fired


@pytest.mark.asyncio
async def test_arm_zero_disables_deadline_and_unregisters():
    """``arm(0.0)`` after a positive arm must drop the deadline from
    the registry — otherwise we leak entries when callers disable a
    previously-armed timer."""
    dl = ConnectionDeadline()
    dl.arm(5.0)
    assert dl in _Scanner._REGISTRY
    dl.arm(0.0)
    assert dl not in _Scanner._REGISTRY
    assert dl._deadline_at == float('inf')
