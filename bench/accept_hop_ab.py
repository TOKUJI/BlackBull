#!/usr/bin/env python3
"""How much does eager task start actually save at the accept site?

``connection_made`` used to schedule the serve coroutine with
``asyncio.create_task``, which queues its first step for a later loop
iteration.  ``asyncio.Task(coro, eager_start=True)`` runs that first step
inline instead.  This measures the difference, and nothing else.

**It is deliberately not an end-to-end benchmark.**  An accepted connection
waits for the peer's first packet either way, so the hop is not on the
latency path — it is bookkeeping (one ``Handle``, one ready-deque round trip,
one ``Handle._run``) that a churn workload pays once per connection.  A
full-server A/B cannot resolve a cost that small: this box's restart-to-restart
throughput is bimodal by ~15%.  So measure the mechanism directly, quote it in
µs per connection, and let the reader divide by BlackBull's own per-request
budget rather than quoting a percentage of some other server's.

Both arms run in one process, interleaved ABBA, after a discarded warm-up.
The A/A null control is the same arm measured against itself — anything the
real comparison reports that is not comfortably outside the null is noise.

Usage::

    python bench/accept_hop_ab.py
    python bench/accept_hop_ab.py --spawns 20000 --rounds 12
"""
from __future__ import annotations

import argparse
import asyncio
import statistics
import time


async def _prologue_then_park(park: asyncio.Event) -> None:
    """Stand-in for ``_serve``: a little work, then a park on the first read.

    The park is what makes this the accept site rather than a task-creation
    microbenchmark — a coroutine that returns immediately would let eager
    start skip machinery the real one still needs.
    """
    await park.wait()


def _queued(park: asyncio.Event) -> asyncio.Task:
    return asyncio.create_task(_prologue_then_park(park))


def _eager(park: asyncio.Event) -> asyncio.Task:
    return asyncio.Task(_prologue_then_park(park),
                        loop=asyncio.get_running_loop(),
                        eager_start=True)


async def _one_pass(spawn, spawns: int) -> float:
    """Seconds to spawn *spawns* serve-shaped tasks and see them all started.

    The queued arm needs a loop turn before its tasks have begun; the eager
    arm does not.  Both are then released and awaited, so each pass accounts
    for the same total work and differs only in when the first step ran.
    """
    park = asyncio.Event()
    t0 = time.perf_counter()
    tasks = [spawn(park) for _ in range(spawns)]
    await asyncio.sleep(0)      # the hop the queued arm needs and eager does not
    park.set()
    await asyncio.gather(*tasks)
    return time.perf_counter() - t0


async def _measure(arms: dict[str, object], spawns: int, rounds: int,
                   ) -> dict[str, list[float]]:
    samples: dict[str, list[float]] = {name: [] for name in arms}
    order = list(arms) + list(reversed(arms))     # ABBA
    for name in order:                            # discarded warm-up
        await _one_pass(arms[name], spawns)
    for _ in range(rounds):
        for name in order:
            samples[name].append(await _one_pass(arms[name], spawns))
    return samples


def _per_conn_us(seconds: list[float], spawns: int) -> tuple[float, float]:
    """Mean and standard error, in microseconds per connection."""
    per = [s / spawns * 1e6 for s in seconds]
    mean = statistics.fmean(per)
    se = statistics.stdev(per) / len(per) ** 0.5 if len(per) > 1 else 0.0
    return mean, se


async def _main(spawns: int, rounds: int) -> None:
    null = await _measure({'A': _queued, "A'": _queued}, spawns, rounds)
    real = await _measure({'queued': _queued, 'eager': _eager}, spawns, rounds)

    print(f'spawns/pass={spawns}  rounds={rounds}  '
          f'passes/arm={rounds * 2}\n')
    print(f'{"arm":<10}{"µs/conn":>12}{"± SE":>10}')
    print('-' * 32)
    rows = {}
    for label, samples in (*null.items(), *real.items()):
        mean, se = _per_conn_us(samples, spawns)
        rows[label] = (mean, se)
        print(f'{label:<10}{mean:>12.3f}{se:>10.3f}')

    a, a_se = rows['A']
    a2, a2_se = rows["A'"]
    q, q_se = rows['queued']
    e, e_se = rows['eager']
    null_delta = a2 - a
    null_se = (a_se ** 2 + a2_se ** 2) ** 0.5
    real_delta = e - q
    real_se = (q_se ** 2 + e_se ** 2) ** 0.5

    print()
    print(f'null  (A\'−A)          {null_delta:+8.3f} ± {null_se:.3f} µs/conn')
    print(f'real  (eager−queued)  {real_delta:+8.3f} ± {real_se:.3f} µs/conn')
    print()
    floor = 2 * (abs(null_delta) + null_se)
    verdict = ('resolved' if abs(real_delta) > floor
               else 'BELOW THE NULL FLOOR — not resolvable here')
    print(f'null floor (2×|null|+SE): {floor:.3f} µs/conn  →  {verdict}')


if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('--spawns', type=int, default=20000)
    ap.add_argument('--rounds', type=int, default=10)
    args = ap.parse_args()
    asyncio.run(_main(args.spawns, args.rounds))
