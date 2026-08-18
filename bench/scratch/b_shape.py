#!/usr/bin/env python3
"""Lever B measured in the shape it would actually take.

The first attempt (attr_shapes.py) compared a class whose instances *never*
set the four counters.  That is not what B creates.  Under B, a stream that
carries a body still sets them, so PEP 412 key sharing puts those keys in the
class's shared table and a body-less instance holds a NULL slot for each — a
different read path from "the class never had this attribute at all", and a
different memory outcome too.

Measured here:
  * read cost on an instance whose slot is NULL (falls back to the class)
    versus one whose slot is filled, with the shared keys populated either way
  * whether the body-less instance is actually any smaller
"""
from __future__ import annotations

import statistics
import sys
import time


class B:
    """The lever: class defaults, instances set them only when a body starts."""
    _body_seen = 0
    _rate_window_start = None
    _rate_window_seen = 0
    _was_window_stalled = False

    def __init__(self, with_body: bool):
        self._queue = None
        self._max_body = 31457280
        self._min_rate = 240.0
        self._min_rate_grace = 5.0
        if with_body:
            self._body_seen = 0
            self._rate_window_start = None
            self._rate_window_seen = 0
            self._was_window_stalled = False


class Today:
    """Status quo: every instance sets all four in __init__."""

    def __init__(self):
        self._queue = None
        self._max_body = 31457280
        self._min_rate = 240.0
        self._min_rate_grace = 5.0
        self._body_seen = 0
        self._rate_window_start = None
        self._rate_window_seen = 0
        self._was_window_stalled = False


def read4(o, n):
    for _ in range(n):
        o._body_seen
        o._rate_window_start
        o._rate_window_seen
        o._was_window_stalled


def _pass(fn, o, n):
    t0 = time.perf_counter()
    fn(o, n)
    return time.perf_counter() - t0


def _ns(xs, n):
    per = [s / n * 1e9 for s in xs]
    return statistics.fmean(per), (statistics.stdev(per) / len(per) ** 0.5
                                   if len(per) > 1 else 0.0)


def compare(label, a_obj, b_obj, n, rounds, per_op):
    def run(arms):
        s = {k: [] for k in arms}
        order = list(arms) + list(reversed(arms))
        for k in order:
            _pass(read4, arms[k], n)
        for _ in range(rounds):
            for k in order:
                s[k].append(_pass(read4, arms[k], n))
        return s
    null = run({'A': a_obj, "A'": a_obj})
    real = run({'base': a_obj, 'treat': b_obj})
    x, xe = _ns(null['A'], n)
    y, ye = _ns(null["A'"], n)
    p, pe = _ns(real['base'], n)
    q, qe = _ns(real['treat'], n)
    nd, nse = y - x, (xe ** 2 + ye ** 2) ** 0.5
    rd, rse = q - p, (pe ** 2 + qe ** 2) ** 0.5
    floor = 2 * (abs(nd) + nse)
    print(f'\n{label}')
    print(f'  base {p:7.2f} ± {pe:.2f} ns    treat {q:7.2f} ± {qe:.2f} ns')
    print(f'  null {nd:+7.2f} ± {nse:.2f}      floor {floor:.2f}')
    print(f'  real {rd:+7.2f} ± {rse:.2f} ns/iter = {rd / per_op:+.2f} ns/read'
          f'  → {"resolved" if abs(rd) > floor else "BELOW NULL FLOOR"}')
    return rd


def main():
    n, rounds = 2_000_000, 8

    # Populate the shared keys table the way a real connection does: some
    # streams carry a body, some do not.
    with_body = B(True)
    without = B(False)
    today = Today()

    print('--- memory, per stream ---')
    for name, o in (('today (all 8 set)', today),
                    ('B, body-less', without),
                    ('B, body-carrying', with_body)):
        d = o.__dict__
        print(f'  {name:<22} attrs={len(d):2d}  '
              f'object={sys.getsizeof(o):3d}  dict={sys.getsizeof(d):4d}  '
              f'total={sys.getsizeof(o) + sys.getsizeof(d):4d} B')

    print('\n--- read cost, 4 counters per iteration ---')
    compare('B body-less (NULL slot → class) vs today (instance value)',
            today, without, n, rounds, 4)
    compare('B body-carrying vs today  (must be ~0: identical shape)',
            today, with_body, n, rounds, 4)


if __name__ == '__main__':
    main()
