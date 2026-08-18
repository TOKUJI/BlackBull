#!/usr/bin/env python3
"""41 -> 27+37: how much is the bug fix, and how much is the split itself?

Three shapes of the same park, counted in executed bytecode instructions
(sys.monitoring — deterministic, no timing noise):

  old      one coroutine, the pre-103 shape: inline pause release, no
           ``_waiting`` flag, the rendezvous created and awaited in place.
  fixed    the same one coroutine, plus the ``_waiting`` flag Sprint 103
           added.  The flag is the *bug fix* — the protocol used to infer
           "somebody is parked" from the rendezvous future, which is cleared
           on wake rather than on stop-waiting, so arrivals in that window
           armed a pause the next park released.
  nested   what 103 shipped: the decision half is a coroutine that awaits the
           execution half, which is another coroutine.
  handoff  the same split, but the protocol's half is *synchronous* — it arms
           and disarms the rendezvous and hands the future back, so the reader
           awaits it directly.  Same two owners, one coroutine.

fixed-old  = the price of the fix          (functionality; unavoidable)
nested-fixed = the price of the split as written
handoff-fixed = the price of the split done without a second coroutine
"""
from __future__ import annotations

import asyncio
import collections
import sys

MON = sys.monitoring
TOOL = MON.PROFILER_ID
_counts: collections.Counter = collections.Counter()
_on = False
_HERE = __file__


def _instr(code, offset):
    if _on and code.co_filename == _HERE:
        _counts[code.co_qualname] += 1
    return None


class Proto:
    def __init__(self):
        self._waiter = None
        self._eof = False
        self._exc = None
        self.reading_paused = False
        self.transport = None

    def _wake(self):
        w, self._waiter = self._waiter, None
        if w is not None and not w.done():
            w.set_result(None)

    def resume_reading(self):
        if self.reading_paused:
            self.reading_paused = False

    # --- old: one coroutine, everything inline ---
    async def old_wait(self):
        if self._exc is not None:
            raise self._exc
        if self._eof:
            return
        if self._waiter is not None:
            raise RuntimeError('not re-entrant')
        if self.reading_paused:
            self.reading_paused = False
        self._waiter = asyncio.get_running_loop().create_future()
        try:
            await self._waiter
        finally:
            self._waiter = None
        if self._exc is not None:
            raise self._exc

    # --- execution half, coroutine (what 103 shipped) ---
    async def wait_for_arrival(self):
        if self._exc is not None:
            raise self._exc
        if self._eof:
            return
        if self._waiter is not None:
            raise RuntimeError('not re-entrant')
        self._waiter = asyncio.get_running_loop().create_future()
        try:
            await self._waiter
        finally:
            self._waiter = None
        if self._exc is not None:
            raise self._exc

    # --- execution half, synchronous handoff ---
    def arm_arrival(self):
        if self._exc is not None:
            raise self._exc
        if self._eof:
            return None
        if self._waiter is not None:
            raise RuntimeError('not re-entrant')
        self._waiter = asyncio.get_running_loop().create_future()
        return self._waiter

    def disarm_arrival(self):
        self._waiter = None
        if self._exc is not None:
            raise self._exc


class Reader:
    def __init__(self, proto):
        self._proto = proto
        self._waiting = False

    # old shape, with the fix's flag added and nothing else
    async def fixed_wait(self):
        proto = self._proto
        if proto._exc is not None:
            raise proto._exc
        if proto._eof:
            return
        if proto._waiter is not None:
            raise RuntimeError('not re-entrant')
        if proto.reading_paused:
            proto.reading_paused = False
        proto._waiter = asyncio.get_running_loop().create_future()
        self._waiting = True
        try:
            await proto._waiter
        finally:
            self._waiting = False
            proto._waiter = None
        if proto._exc is not None:
            raise proto._exc

    # what 103 shipped
    async def nested_wait(self):
        proto = self._proto
        if proto.reading_paused:
            proto.resume_reading()
        self._waiting = True
        try:
            await proto.wait_for_arrival()
        finally:
            self._waiting = False

    # same split, synchronous handoff
    async def handoff_wait(self):
        proto = self._proto
        if proto.reading_paused:
            proto.resume_reading()
        fut = proto.arm_arrival()
        if fut is None:
            return
        self._waiting = True
        try:
            await fut
        finally:
            self._waiting = False
            proto.disarm_arrival()


async def _drive(coro_factory, proto, n):
    """Park n times, waking each park from the loop as the transport does."""
    loop = asyncio.get_running_loop()
    for _ in range(n):
        t = asyncio.ensure_future(coro_factory())
        await asyncio.sleep(0)          # let it reach the await
        loop.call_soon(proto._wake)
        await t


async def _main(n):
    global _on
    proto = Proto()
    reader = Reader(proto)
    shapes = {
        'old': lambda: proto.old_wait(),
        'fixed': lambda: reader.fixed_wait(),
        'nested': lambda: reader.nested_wait(),
        'handoff': lambda: reader.handoff_wait(),
    }
    for f in shapes.values():           # warm-up, unmonitored
        await _drive(f, proto, 20)

    MON.use_tool_id(TOOL, 'park')
    MON.register_callback(TOOL, MON.events.INSTRUCTION, _instr)
    MON.set_events(TOOL, MON.events.INSTRUCTION)

    results = {}
    for name, f in shapes.items():
        _counts.clear()
        _on = True
        await _drive(f, proto, n)
        _on = False
        # _drive itself is in this file; subtract its constant contribution
        results[name] = sum(v for k, v in _counts.items()
                            if not k.startswith('_drive'))
        print(f'  {name}: ' + ', '.join(
            f'{k.split(".")[-1]}={v/n:.1f}' for k, v in sorted(_counts.items())
            if not k.startswith('_drive')))
    MON.set_events(TOOL, 0)
    MON.free_tool_id(TOOL)

    print(f'parks measured per shape: {n}\n')
    print(f'{"shape":<10}{"instr/park":>12}')
    print('-' * 22)
    for k, v in results.items():
        print(f'{k:<10}{v / n:>12.1f}')

    old, fixed = results['old'] / n, results['fixed'] / n
    nested, handoff = results['nested'] / n, results['handoff'] / n
    print()
    print(f'the bug fix  (fixed - old)      {fixed - old:+7.1f} instr/park'
          f'   ← functionality')
    print(f'split as written (nested-fixed) {nested - fixed:+7.1f} instr/park'
          f'   ← second coroutine')
    print(f'split via handoff (handoff-fixed){handoff - fixed:+6.1f} instr/park'
          f'   ← same owners, one coroutine')
    print(f'recoverable by refactoring       {nested - handoff:+7.1f} instr/park')


if __name__ == '__main__':
    asyncio.run(_main(2000))
