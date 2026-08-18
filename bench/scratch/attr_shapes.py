#!/usr/bin/env python3
"""What each attribute shape costs to read and to write, on this interpreter.

Levers B and C both make a body-*less* stream cheaper by changing where a
value lives.  Both are read again on the body path — B's counters and C's
limits are touched per DATA frame — so the question that decides them is not
"how much does construction save" but "what does the read cost afterwards".

Shapes:
  instance   self._x            — today
  classattr  self._x, where _x is a class default and no instance entry exists
  indirect   self._limits.x     — lever C

ABBA interleaved with an A/A null, as bench/accept_hop_ab.py does.
"""
from __future__ import annotations

import argparse
import statistics
import time


class Instance:
    def __init__(self):
        self._max_body = 31457280
        self._min_rate = 240.0
        self._min_rate_grace = 5.0
        self._was_window_stalled = False


class ClassAttr:
    _max_body = 31457280
    _min_rate = 240.0
    _min_rate_grace = 5.0
    _was_window_stalled = False

    def __init__(self):
        pass


class _Limits:
    __slots__ = ('max_body', 'min_rate', 'min_rate_grace')

    def __init__(self):
        self.max_body = 31457280
        self.min_rate = 240.0
        self.min_rate_grace = 5.0


class Indirect:
    def __init__(self, limits):
        self._limits = limits
        self._was_window_stalled = False


def read_instance(o, n):
    for _ in range(n):
        o._max_body
        o._min_rate
        o._min_rate_grace


def read_classattr(o, n):
    for _ in range(n):
        o._max_body
        o._min_rate
        o._min_rate_grace


def read_indirect(o, n):
    for _ in range(n):
        o._limits.max_body
        o._limits.min_rate
        o._limits.min_rate_grace


def read_flag_instance(o, n):
    for _ in range(n):
        o._was_window_stalled


def read_flag_classattr(o, n):
    for _ in range(n):
        o._was_window_stalled


def _pass(fn, obj, n):
    t0 = time.perf_counter()
    fn(obj, n)
    return time.perf_counter() - t0


def _measure(arms, n, rounds):
    samples = {k: [] for k in arms}
    order = list(arms) + list(reversed(arms))
    for k in order:
        _pass(*arms[k], n)
    for _ in range(rounds):
        for k in order:
            samples[k].append(_pass(*arms[k], n))
    return samples


def _ns(seconds, n):
    per = [s / n * 1e9 for s in seconds]
    m = statistics.fmean(per)
    se = statistics.stdev(per) / len(per) ** 0.5 if len(per) > 1 else 0.0
    return m, se


def compare(label, base, treat, n, rounds, per_op):
    null = _measure({'A': base, "A'": base}, n, rounds)
    real = _measure({'base': base, 'treat': treat}, n, rounds)
    a, ase = _ns(null['A'], n)
    a2, a2se = _ns(null["A'"], n)
    b, bse = _ns(real['base'], n)
    t, tse = _ns(real['treat'], n)
    nd, nse = a2 - a, (ase ** 2 + a2se ** 2) ** 0.5
    rd, rse = t - b, (bse ** 2 + tse ** 2) ** 0.5
    floor = 2 * (abs(nd) + nse)
    ok = abs(rd) > floor
    print(f'\n{label}   ({per_op} reads per iteration)')
    print(f'  base {b:8.2f} ± {bse:.2f} ns    treat {t:8.2f} ± {tse:.2f} ns')
    print(f'  null {nd:+8.2f} ± {nse:.2f}      floor {floor:.2f}')
    print(f'  real {rd:+8.2f} ± {rse:.2f} ns/iter  '
          f'= {rd / per_op:+.2f} ns per read  '
          f'→ {"resolved" if ok else "BELOW NULL FLOOR"}')
    return rd / per_op if ok else 0.0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--iters', type=int, default=2_000_000)
    ap.add_argument('--rounds', type=int, default=8)
    a = ap.parse_args()

    inst, cls = Instance(), ClassAttr()
    ind = Indirect(_Limits())

    print(f'iters={a.iters:,}  rounds={a.rounds}')
    c = compare('C: self._limits.x  vs  self._x',
                (read_instance, inst), (read_indirect, ind),
                a.iters, a.rounds, 3)
    b = compare('B: class-attribute read  vs  instance read (3 limits)',
                (read_instance, inst), (read_classattr, cls),
                a.iters, a.rounds, 3)
    f = compare('B: class-attribute read  vs  instance read (1 flag)',
                (read_flag_instance, inst), (read_flag_classattr, cls),
                a.iters, a.rounds, 1)

    print('\n--- what that means per DATA frame ---')
    print(f'C  adds {c * 3:+.2f} ns per DATA frame (3 limit reads)')
    print(f'B  adds {f:+.2f} ns per DATA frame for _was_window_stalled '
          f'while it stays class-resident')
    print(f'   (3-limit class read delta, for reference: {b * 3:+.2f} ns)')


if __name__ == '__main__':
    main()


# --- appended: what the construction-side saving is actually worth ----------

class _Ctor:
    def three(self, a, b, c):
        self._max_body = a
        self._min_rate = b
        self._min_rate_grace = c

    def one(self, limits):
        self._limits = limits


def _stores_three(o, n):
    for _ in range(n):
        o.three(1, 2.0, 3.0)


def _stores_one(o, n):
    for _ in range(n):
        o.one(None)


def store_cost(iters=2_000_000, rounds=8):
    o = _Ctor()
    d = compare('C saving: 3 stores  vs  1 store (per stream)',
                (_stores_three, o), (_stores_one, o), iters, rounds, 1)
    print(f'\nC saves {-d:.2f} ns per stream and costs 4.99 ns per DATA frame')
    if d:
        print(f'break-even at {-d / 4.99:.1f} DATA frames')
