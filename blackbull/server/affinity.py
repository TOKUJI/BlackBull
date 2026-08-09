"""Per-worker CPU placement.

Pinning a worker's event loop to one core keeps its hot state — the header
line table, the HPACK dynamic tables, the connection dict — resident in that
core's L1/L2 instead of following the thread around the machine.  That is the
whole of the upside, and it is real; the care in this module is all about the
three ways a naive pin does damage:

**Never widen the mask we were given.**  ``sched_setaffinity`` lets a process
move itself onto CPUs its own mask excludes, so a pin computed against
``os.cpu_count()`` silently overrides ``taskset``, ``numactl``, and any
cpuset the operator placed us in.  Every placement here is drawn from
``sched_getaffinity`` — an operator's confinement is an input, not an
obstacle.

**Never pin the thread pool.**  Linux threads inherit the creating thread's
affinity mask.  Pinning the main thread and then offloading compression or a
file read to the default executor puts that work on the one core the event
loop is already saturating, which is precisely backwards.
:func:`make_offload_executor` hands each pool thread the full mask back.

**Always be switchable off.**  On a shared or externally-orchestrated host the
right number of pinning decisions for a framework to make is zero.
"""
from __future__ import annotations

import concurrent.futures
import logging
import os

logger = logging.getLogger(__name__)

#: Spec values that mean "leave placement to the operator".
_OFF = frozenset({'', 'off', 'none', '0'})

#: Spec value that means "one worker per available CPU, in order".
_AUTO = 'auto'


def _parse_cpu_list(spec: str) -> set[int] | None:
    """Parse ``taskset``-style ``2,4,6-9`` into a CPU set, or ``None`` if the
    text is not a well-formed list.

    Deliberately strict — a typo in a deployment variable should announce
    itself rather than resolve to some neighbouring core.
    """
    cpus: set[int] = set()
    for field in spec.split(','):
        field = field.strip()
        if not field:
            return None
        lo_text, sep, hi_text = field.partition('-')
        if not lo_text.isdigit() or (sep and not hi_text.isdigit()):
            return None
        lo = int(lo_text)
        hi = int(hi_text) if sep else lo
        if hi < lo:
            return None
        cpus.update(range(lo, hi + 1))
    return cpus


def resolve_worker_cpus(spec: str, worker_id: int,
                        allowed: frozenset[int]) -> frozenset[int] | None:
    """CPUs worker *worker_id* should run on, or ``None`` to leave it alone.

    *allowed* is the mask the process already carries — the placement the
    operator chose.  The result is always a subset of it.
    """
    normalised = spec.strip().lower()
    if normalised in _OFF:
        return None
    if not allowed:
        logger.warning('BB_CPU_AFFINITY=%s: no CPUs available to pin to; '
                       'leaving placement unchanged', spec)
        return None

    if normalised == _AUTO:
        pool = sorted(allowed)
    else:
        requested = _parse_cpu_list(normalised)
        if requested is None:
            logger.warning('BB_CPU_AFFINITY=%r is not a CPU list '
                           "('auto', 'off', or e.g. '2,4,6-9'); "
                           'leaving placement unchanged', spec)
            return None
        pool = sorted(requested & allowed)
        if not pool:
            logger.warning('BB_CPU_AFFINITY=%r selects no CPU this process is '
                           'allowed to run on (available: %s); leaving '
                           'placement unchanged', spec, sorted(allowed))
            return None

    # Round-robin rather than one-to-one: workers may outnumber cores, and
    # sharing a core is a better answer than refusing to start.
    return frozenset({pool[worker_id % len(pool)]})


def apply_worker_affinity(worker_id: int, spec: str) -> frozenset[int] | None:
    """Pin this thread for worker *worker_id*; return the mask it had before.

    The return value is what :func:`make_offload_executor` needs — the
    placement the operator gave us, which offloaded work should keep even
    though the event loop no longer does.  ``None`` means nothing was pinned
    and no executor override is warranted.
    """
    if not hasattr(os, 'sched_setaffinity'):
        return None

    allowed = frozenset(os.sched_getaffinity(0))
    target = resolve_worker_cpus(spec, worker_id, allowed)
    if target is None:
        return None

    try:
        os.sched_setaffinity(0, target)
    except OSError as exc:
        logger.warning('worker %d: CPU affinity pinning unavailable: %s',
                       worker_id, exc)
        return None

    logger.debug('worker %d pinned to CPU %s (of %s)',
                 worker_id, sorted(target), sorted(allowed))
    # Only worth overriding the executor when the pin actually narrowed
    # something; a single-CPU box pins to the mask it already had.
    return allowed if allowed != target else None


def make_offload_executor(allowed: frozenset[int],
                          max_workers: int | None = None,
                          ) -> concurrent.futures.ThreadPoolExecutor:
    """A thread pool whose threads run on *allowed*, not on the loop's pin.

    A thread that cannot set the mask still runs its work: losing the spread
    costs throughput, refusing to start the thread costs the request.
    """
    def _unpin() -> None:
        try:
            os.sched_setaffinity(0, allowed)
        except OSError as exc:
            logger.debug('offload thread kept the inherited CPU mask: %s', exc)

    return concurrent.futures.ThreadPoolExecutor(
        max_workers=max_workers,
        thread_name_prefix='bb-offload',
        initializer=_unpin,
    )
