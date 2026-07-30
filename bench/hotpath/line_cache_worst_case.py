#!/usr/bin/env python3
"""The header-line cache's worst case, stated in numbers rather than adjectives.

Every other harness here measures where the cache is *meant* to help.  This one
measures where it cannot help and can only cost, because a bound that has only
been exercised on friendly input is not a bound that has been tested.

The cache key is attacker-controlled, so the adversary's move is obvious: send
header lines that are (a) always unique, so every lookup misses, and (b) as
large as the limits allow, so each miss pays a hash over the maximum number of
bytes and each admission retains the maximum number of bytes.

Two costs, measured separately because they have different shapes:

**CPU** — pure waste per request: hash the line, miss, and (until the cache
fills) insert.  Compared against a build with no cache at all.

**Memory** — the one that does not show up in a throughput benchmark at all.
Without the cache, header bytes are transient and freed after the parse.  With
it, up to ``_LINE_CACHE_MAX`` lines stay reachable for the lifetime of the
connection — and an entry retains the key *and* the name/value slices.  An
attacker pays that cost once, in traffic, and the server pays it continuously,
in resident memory, for as long as the connection is kept alive.

    python bench/hotpath/line_cache_worst_case.py
"""
from __future__ import annotations

import argparse
import pathlib
import sys
import time
import tracemalloc

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[0]))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

from provenance import stamp  # noqa: E402

from blackbull.env import get_settings  # noqa: E402
from blackbull.server.http1_actor import HTTP1Actor  # noqa: E402

try:
    from blackbull.server.http1_actor import _LINE_CACHE_MAX
except ImportError:                      # a build without the cache
    _LINE_CACHE_MAX = 0


def _actor() -> HTTP1Actor:
    a = HTTP1Actor.__new__(HTTP1Actor)
    a._ssl = False
    return a


def hostile_request(seq: int, n_lines: int, line_len: int) -> bytes:
    """*n_lines* maximally long, never-repeating header lines."""
    out = [b'GET /x HTTP/1.1', b'Host: h']
    for i in range(n_lines):
        name = b'X-P%d' % i
        # A unique prefix per request guarantees a miss; the padding makes the
        # hash on that miss as expensive as the line limit allows.
        pad = b'a' * max(0, line_len - len(name) - 24)
        out.append(b'%s: %012d-%08d-%s' % (name, seq, i, pad))
    return b'\r\n'.join(out) + b'\r\n\r\n'


def bench_cpu(n_lines: int, line_len: int, iters: int) -> float:
    actor = _actor()          # one connection, kept alive
    reqs = [hostile_request(i, n_lines, line_len) for i in range(iters)]
    best = []
    for _ in range(5):
        t0 = time.perf_counter_ns()
        for raw in reqs:
            actor._parse(raw)
        best.append((time.perf_counter_ns() - t0) / iters / 1000)
    return min(best)


def measure_memory(n_lines: int, line_len: int) -> tuple[int, int]:
    """Bytes retained by one connection's cache: (accounted, tracemalloc)."""
    actor = _actor()
    tracemalloc.start()
    before = tracemalloc.get_traced_memory()[0]
    seq = 0
    # Feed until the cache stops growing (or forever-loop guard trips).
    while seq < 400:
        actor._parse(hostile_request(seq, n_lines, line_len))
        cache = getattr(actor, '_line_cache', None)
        if cache is not None and len(cache) >= _LINE_CACHE_MAX:
            break
        seq += 1
    after = tracemalloc.get_traced_memory()[0]
    tracemalloc.stop()

    cache = getattr(actor, '_line_cache', None) or {}
    accounted = sum(len(k) + len(v[0]) + len(v[1]) for k, v in cache.items())
    return accounted, max(0, after - before)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--iters', type=int, default=300)
    args = ap.parse_args()

    print(stamp() + '\n')
    cfg = get_settings()
    print(f'limits: header_max_line={cfg.header_max_line}  '
          f'header_max_total={cfg.header_max_total}  '
          f'_LINE_CACHE_MAX={_LINE_CACHE_MAX}\n')

    # The adversary picks the line length, so sweep it rather than assume one.
    # A per-line admission cap moves the worst case *down* to just under that
    # cap — long lines stop being cached at all — so the maximum has to be
    # searched for, not guessed.
    lengths = [64, 128, 256, 512, 1000, 1024, 1025, 2048, 4096,
               cfg.header_max_line]
    print(f'{"line B":>8} {"lines/req":>10} {"CPU µs/req":>11} '
          f'{"retained B":>12} {"KiB/conn":>9} {"GiB @10k":>9}')
    worst = (0, 0)
    for line_len in lengths:
        n_lines = max(1, (cfg.header_max_total - 64) // (line_len + 2))
        n_lines = min(n_lines, 64)         # keep the sweep's cost sane
        cpu = bench_cpu(n_lines, line_len, args.iters)
        accounted, _ = measure_memory(n_lines, line_len)
        print(f'{line_len:>8} {n_lines:>10} {cpu:>11.2f} {accounted:>12,d} '
              f'{accounted / 1024:>9.1f} {accounted * 10_000 / 1024**3:>9.2f}')
        worst = max(worst, (accounted, line_len))

    print(f'\nworst retention: {worst[0]:,d} B/conn at {worst[1]} B lines '
          f'-> {worst[0] * 10_000 / 1024**2:.0f} MiB at 10k connections')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
