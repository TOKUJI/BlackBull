#!/usr/bin/env python3
"""What the validated header-line cache costs when it never hits.

`parser_micro.py` reuses one actor and one byte-identical request, which is the
keep-alive case the cache is designed for and therefore its best case.  This is
the other end: every request carries header *values* this connection has never
seen, so every lookup misses and the dict work is pure overhead.  A real client
sits between the two, but the sprint should not ship a win it has only measured
on the favourable side.

Three arms, all on one actor (one connection):

  hit      byte-identical requests — the keep-alive shape
  miss     every value unique — the cache is admitted-then-never-reused
  miss-full  unique values *after* the cache is full, so admission is refused
             and the `len(cache) < MAX` branch is what runs

    python bench/hotpath/line_cache_miss.py [--iters N] [--headers N]
"""
from __future__ import annotations

import argparse
import pathlib
import statistics
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[0]))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

from provenance import stamp  # noqa: E402

from blackbull.server.http1_actor import (  # noqa: E402
    _LINE_CACHE_MAX,
    HTTP1Actor,
)

NAMES = [
    b'User-Agent', b'Accept', b'Accept-Encoding', b'Accept-Language',
    b'Cookie', b'Referer', b'Origin', b'X-Request-Id',
]


def _actor() -> HTTP1Actor:
    a = HTTP1Actor.__new__(HTTP1Actor)
    a._ssl = False
    return a


def request_bytes(n: int, salt: int | None) -> bytes:
    lines = [b'GET /baseline11?a=1 HTTP/1.1', b'Host: 127.0.0.1:8501']
    for i in range(n):
        name = NAMES[i % len(NAMES)] + (b'-%d' % (i // len(NAMES)))
        value = b'v' if salt is None else b'v%d-%d' % (salt, i)
        lines.append(name + b': ' + value)
    return b'\r\n'.join(lines) + b'\r\n\r\n'


def bench(fn, iters: int) -> float:
    best = []
    for _ in range(7):
        t0 = time.perf_counter_ns()
        for i in range(iters):
            fn(i)
        best.append((time.perf_counter_ns() - t0) / iters / 1000)
    return min(best)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--iters', type=int, default=20000)
    ap.add_argument('--headers', type=int, default=8)
    args = ap.parse_args()
    print(stamp() + '\n')
    n = args.headers

    hit_actor = _actor()
    same = request_bytes(n, None)
    hit_actor._parse(same)
    hit = bench(lambda _i: hit_actor._parse(same), args.iters)

    miss_actor = _actor()
    miss = bench(lambda i: miss_actor._parse(request_bytes(n, i)), args.iters)

    full_actor = _actor()
    # Fill the cache with lines that will never be seen again.
    for i in range(_LINE_CACHE_MAX + 8):
        full_actor._parse(request_bytes(n, 10_000_000 + i))
    assert len(full_actor._line_cache) == _LINE_CACHE_MAX
    full = bench(lambda i: full_actor._parse(request_bytes(n, i)), args.iters)

    # The miss arms rebuild the request bytes inside the timed loop; charge
    # that to neither by measuring it alone and subtracting.
    build = bench(lambda i: request_bytes(n, i), args.iters)

    print(f'{n} headers, {args.iters} iters (min of 7)\n')
    print(f'  hit        {hit:6.2f} µs')
    print(f'  miss       {miss - build:6.2f} µs   '
          f'({(miss - build) / hit - 1:+.1%} vs hit)')
    print(f'  miss-full  {full - build:6.2f} µs   '
          f'({(full - build) / hit - 1:+.1%} vs hit)')
    print(f'\n  (request construction, subtracted: {build:.2f} µs)')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
