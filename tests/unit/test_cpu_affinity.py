"""Per-worker CPU placement — :mod:`blackbull.server.affinity`.

The behaviours asserted here are the three ways the original unconditional
pin misbehaved, plus the one way it behaved correctly (which must not
change):

1. It escaped the operator's placement.  ``taskset -c 8-11`` sets the
   process affinity mask, but a process may *widen* its own mask again, so
   ``sched_setaffinity(0, {worker_id % os.cpu_count()})`` cheerfully moved
   worker 0 onto CPU 0 — outside the set the operator asked for.
2. It pinned the thread-pool too.  Linux threads inherit the creating
   thread's affinity mask, so every ``run_in_executor`` compression offload
   and every ``asyncio.to_thread`` file read landed on the one core the
   event loop was already saturating — the opposite of what offloading is
   for.
3. It could not be turned off.
4. On an unrestricted box it placed worker *i* on CPU ``i % cpu_count``.
   That placement is the part worth keeping, and
   ``test_auto_reproduces_the_historical_placement`` is what keeps it.
"""
from __future__ import annotations

import concurrent.futures
import logging
import os
import time

import pytest

from blackbull.server.affinity import (make_offload_executor,
                                       resolve_worker_cpus)

# A mask an operator would produce with ``taskset -c 8-11`` / a cpuset cgroup:
# contiguous, non-zero-based, and a strict subset of a 12-core box.
RESTRICTED = frozenset({8, 9, 10, 11})
UNRESTRICTED = frozenset(range(12))


# ---------------------------------------------------------------------------
# resolve_worker_cpus — the placement decision, no syscalls
# ---------------------------------------------------------------------------

@pytest.mark.parametrize('spec', ['off', 'OFF', '', '   '])
def test_off_leaves_placement_alone(spec):
    assert resolve_worker_cpus(spec, 0, UNRESTRICTED) is None


def test_auto_reproduces_the_historical_placement():
    """On an unrestricted mask ``auto`` must pin exactly where the original
    unconditional code pinned — otherwise the default is a perf change
    wearing a bug fix's clothes."""
    for worker_id in range(20):
        historical = {worker_id % len(UNRESTRICTED)}
        assert resolve_worker_cpus('auto', worker_id, UNRESTRICTED) == historical


def test_auto_never_leaves_the_allowed_set():
    """The escape defect: pinning must draw from the mask we were given."""
    for worker_id in range(20):
        cpus = resolve_worker_cpus('auto', worker_id, RESTRICTED)
        assert cpus is not None
        assert cpus <= RESTRICTED, f'worker {worker_id} escaped to {cpus}'


def test_auto_spreads_workers_across_the_allowed_set():
    """One worker per allowed CPU before any core is reused."""
    placements = [resolve_worker_cpus('auto', i, RESTRICTED)
                  for i in range(len(RESTRICTED))]
    assert {c for p in placements for c in p} == set(RESTRICTED)


def test_zero_is_cpu_zero_not_an_off_switch():
    """``0`` is a valid CPU index, not the disable sentinel — a numeric
    domain must not hand a number to the off switch.  ``off`` disables;
    ``0`` pins to CPU 0."""
    assert resolve_worker_cpus('0', 0, UNRESTRICTED) == {0}
    assert resolve_worker_cpus('0', 3, UNRESTRICTED) == {0}
    assert resolve_worker_cpus('0, 2', 0, UNRESTRICTED) == {0}


@pytest.mark.parametrize(('spec', 'expected'), [
    ('9', [{9}, {9}, {9}]),
    ('8,10', [{8}, {10}, {8}]),
    ('9-11', [{9}, {10}, {11}]),
    ('8, 10-11', [{8}, {10}, {11}]),
])
def test_explicit_list_uses_taskset_syntax(spec, expected):
    got = [resolve_worker_cpus(spec, i, RESTRICTED) for i in range(len(expected))]
    assert got == expected


def test_explicit_list_is_intersected_with_the_allowed_set():
    """Asking for CPUs the operator did not grant us cannot grant them."""
    cpus = resolve_worker_cpus('0-11', 0, RESTRICTED)
    assert cpus is not None
    assert cpus <= RESTRICTED


def test_explicit_list_disjoint_from_allowed_declines_to_pin(caplog):
    """Nothing in common: leave placement alone rather than raise or escape."""
    with caplog.at_level(logging.WARNING, logger='blackbull.server.affinity'):
        assert resolve_worker_cpus('0-3', 0, RESTRICTED) is None
    assert 'BB_CPU_PINNING' in caplog.text


def test_an_absurd_range_costs_nothing_to_reject():
    """A mistyped upper bound must not be materialised.  Every CPU above the
    mask is discarded by the intersection anyway, so building the range first
    buys nothing and costs a gigabyte: ``0-20000000`` allocated 1.15 GiB and
    took 1.8 s *per worker*, at fork time, before being thrown away."""
    before = time.perf_counter()
    cpus = resolve_worker_cpus('0-20000000', 0, RESTRICTED)
    elapsed = time.perf_counter() - before

    assert cpus == {8}, 'the clamp must not change which CPU is chosen'
    assert elapsed < 0.5, f'took {elapsed:.2f}s — the range was materialised'


def test_clamping_does_not_swallow_a_malformed_range(caplog):
    """The bound is applied after validation, so a reversed range is still
    reported rather than quietly clamped into something plausible."""
    with caplog.at_level(logging.WARNING, logger='blackbull.server.affinity'):
        assert resolve_worker_cpus('11-8', 0, RESTRICTED) is None
    assert 'not a CPU list' in caplog.text


@pytest.mark.parametrize('spec', ['sixteen', '3-', '-3', '4-2', '1,,2', '-1'])
def test_malformed_spec_declines_to_pin(spec, caplog):
    with caplog.at_level(logging.WARNING, logger='blackbull.server.affinity'):
        assert resolve_worker_cpus(spec, 0, UNRESTRICTED) is None
    assert 'BB_CPU_PINNING' in caplog.text


def test_empty_allowed_set_declines_to_pin():
    """Defensive: an empty mask is not something to divide by."""
    assert resolve_worker_cpus('auto', 0, frozenset()) is None


# ---------------------------------------------------------------------------
# make_offload_executor — the thread-inheritance defect
# ---------------------------------------------------------------------------

@pytest.mark.skipif(not hasattr(os, 'sched_setaffinity'),
                    reason='CPU affinity is Linux-only')
def test_offload_threads_get_the_full_mask_not_the_pin():
    """Offloaded work must run on every CPU the operator granted us, not on
    the single core the event loop is pinned to."""
    allowed = frozenset(os.sched_getaffinity(0))
    if len(allowed) < 2:
        pytest.skip('needs at least two available CPUs to tell the two apart')
    pinned = {min(allowed)}

    original = set(allowed)
    try:
        os.sched_setaffinity(0, pinned)
        # The control: a plain executor inherits the pin.
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as plain:
            inherited = plain.submit(os.sched_getaffinity, 0).result()
        assert set(inherited) == pinned

        with make_offload_executor(allowed, max_workers=1) as ex:
            restored = ex.submit(os.sched_getaffinity, 0).result()
        assert set(restored) == set(allowed)
    finally:
        os.sched_setaffinity(0, original)


@pytest.mark.skipif(not hasattr(os, 'sched_setaffinity'),
                    reason='CPU affinity is Linux-only')
def test_offload_executor_survives_a_mask_it_cannot_set():
    """A worker thread that cannot restore the mask still has to run the
    work — an unusable executor is worse than an unpinned one."""
    unavailable = frozenset({1 << 20})
    with make_offload_executor(unavailable, max_workers=1) as ex:
        assert ex.submit(lambda: 'ran').result() == 'ran'


# ---------------------------------------------------------------------------
# run_worker wiring — the two behaviours an operator can actually observe
# ---------------------------------------------------------------------------

@pytest.fixture()
def worker_harness(monkeypatch):
    """Run ``run_worker`` against a stub server that records what the loop and
    the offload pool see, then puts the process's CPU mask back."""
    import asyncio
    import signal

    import blackbull.server.server as server_module
    from blackbull.env import reset_settings_cache

    observed: dict = {}

    class _StubServer:
        def __init__(self, *_args, **_kwargs):
            self.raw_sockets = []
            self.port = 0

        async def run(self):
            observed['loop_thread'] = set(os.sched_getaffinity(0))
            observed['offload_thread'] = set(
                await asyncio.to_thread(os.sched_getaffinity, 0))

    monkeypatch.setattr(server_module, 'ASGIServer', _StubServer)
    monkeypatch.setenv('BB_ASYNC_LOGGING', '0')

    original_mask = set(os.sched_getaffinity(0))
    handlers = {s: signal.getsignal(s) for s in (signal.SIGINT, signal.SIGTERM)}
    reset_settings_cache()
    try:
        yield observed
    finally:
        for sig, handler in handlers.items():
            signal.signal(sig, handler)
        os.sched_setaffinity(0, original_mask)
        reset_settings_cache()


@pytest.mark.skipif(not hasattr(os, 'sched_setaffinity'),
                    reason='CPU affinity is Linux-only')
def test_worker_off_leaves_the_process_mask_untouched(worker_harness, monkeypatch):
    from blackbull.server.worker import run_worker

    monkeypatch.setenv('BB_CPU_PINNING', 'off')
    before = set(os.sched_getaffinity(0))
    run_worker(object(), [], None, worker_id=0, max_connections=0)
    assert worker_harness['loop_thread'] == before


@pytest.mark.skipif(not hasattr(os, 'sched_setaffinity'),
                    reason='CPU affinity is Linux-only')
def test_worker_pins_the_loop_but_not_the_offload_pool(worker_harness, monkeypatch):
    from blackbull.server.worker import run_worker

    allowed = set(os.sched_getaffinity(0))
    if len(allowed) < 2:
        pytest.skip('needs at least two available CPUs to tell the two apart')

    monkeypatch.setenv('BB_CPU_PINNING', 'auto')
    run_worker(object(), [], None, worker_id=1, max_connections=0)

    assert worker_harness['loop_thread'] == {sorted(allowed)[1]}
    assert worker_harness['offload_thread'] == allowed
