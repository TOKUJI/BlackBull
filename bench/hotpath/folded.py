#!/usr/bin/env python3
"""Summarise a py-spy raw (folded) profile: self time and inclusive time.

A folded line is `<frame>;<frame>;...;<leaf> <count>`, one per distinct
stack.  Self time is the leaf's share; inclusive time is every stack the
frame appears anywhere in.  Import-time stacks are dropped — they are
startup, not the request hot path.
"""
import collections
import re
import sys

DROP = ('_find_and_load', 'exec_module')


def load(path):
    self_t = collections.Counter()
    incl = collections.Counter()
    total = 0
    for line in open(path):
        line = line.rstrip('\n')
        if not line:
            continue
        stack, _, cnt = line.rpartition(' ')
        try:
            n = int(cnt)
        except ValueError:
            continue
        stack = re.sub(r'^process \d+:"[^"]*";', '', stack)
        frames = [f for f in stack.split(';') if f]
        if not frames or any(d in f for f in frames for d in DROP):
            continue
        total += n
        self_t[frames[-1]] += n
        for f in set(frames):
            incl[f] += n
    return total, self_t, incl


def show(path, top=30):
    total, self_t, incl = load(path)
    print(f'== {path}  ({total} request-path samples) ==')
    print(f'{"self%":>7} {"incl%":>7}  frame')
    for f, n in self_t.most_common(top):
        print(f'{100*n/total:7.2f} {100*incl[f]/total:7.2f}  {f}')
    return total, self_t, incl


if __name__ == '__main__':
    for p in sys.argv[1:]:
        show(p)
        print()
