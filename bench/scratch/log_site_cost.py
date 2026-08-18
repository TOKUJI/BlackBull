#!/usr/bin/env python3
"""What one disabled ``logger.debug(...)`` costs, in executed instructions.

The codebase already holds the principle: ``@log`` inspects the level at
*decoration* time and returns the function unwrapped, "a zero-cost no-op in
production".  The inline call sites do not — they call ``Logger.debug``, which
calls ``isEnabledFor``, which checks a cache, and then returns.  Nothing is
emitted and the whole round trip is per request.

Three shapes, counted with sys.monitoring so the answer is exact:

  bare      logger.debug('...', a, b)          — today
  guarded   if logger.isEnabledFor(DEBUG): ... — one call instead of two
  flag      if _DEBUG: ...                     — module constant read at import,
                                                 the shape ``@log`` already uses
"""
from __future__ import annotations

import collections
import logging
import sys

logging.getLogger().setLevel(logging.WARNING)
logger = logging.getLogger('blackbull.bench.logsite')
logger.setLevel(logging.WARNING)

_DEBUG = logger.isEnabledFor(logging.DEBUG)

MON = sys.monitoring
TOOL = MON.PROFILER_ID
_counts: collections.Counter = collections.Counter()
_on = False


def _instr(code, offset):
    if _on:
        _counts[code.co_filename] += 1
    return None


def bare(n, a, b):
    for _ in range(n):
        logger.debug('frame %s on stream %s', a, b)


def bare_tuple(n, a, b):
    """The app.py shape: an eagerly-built tuple as the sole argument."""
    for _ in range(n):
        logger.debug((a, b))


def guarded(n, a, b):
    for _ in range(n):
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug('frame %s on stream %s', a, b)


def flag(n, a, b):
    for _ in range(n):
        if _DEBUG:
            logger.debug('frame %s on stream %s', a, b)


def nothing(n, a, b):
    for _ in range(n):
        pass


def measure(fn, n=200_000):
    global _on
    _counts.clear()
    _on = True
    fn(n, 'DATA', 5)
    _on = False
    return sum(_counts.values()) / n


def main():
    MON.use_tool_id(TOOL, 'logsite')
    MON.register_callback(TOOL, MON.events.INSTRUCTION, _instr)
    MON.set_events(TOOL, MON.events.INSTRUCTION)

    base = measure(nothing)
    shapes = {'bare (%-args)': bare, 'bare (tuple arg)': bare_tuple,
              'guarded': guarded, 'flag': flag}
    results = {k: measure(v) - base for k, v in shapes.items()}

    MON.set_events(TOOL, 0)
    MON.free_tool_id(TOOL)

    print(f'{"shape":<20}{"instr/site":>12}')
    print('-' * 32)
    for k, v in results.items():
        print(f'{k:<20}{v:>12.1f}')

    bare_c = results['bare (%-args)']
    print()
    print(f'{"":22}{"saving/site":>13}{"H2 (20/req)":>14}{"/conn (3/req)":>15}')
    for k in ('guarded', 'flag'):
        s = bare_c - results[k]
        print(f'{k:<22}{s:>13.1f}{s * 20:>13.0f}i{s * 3:>14.0f}i')
    print()
    print('lane budgets: H2 8924.6 instr/req, /conn 3795.9 instr/req')
    for k in ('guarded', 'flag'):
        s = bare_c - results[k]
        print(f'  {k:<10} H2 {s * 20 / 8924.6 * 100:5.2f} %   '
              f'/conn {s * 3 / 3795.9 * 100:5.2f} %')


if __name__ == '__main__':
    main()
