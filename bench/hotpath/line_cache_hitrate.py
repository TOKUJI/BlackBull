#!/usr/bin/env python3
"""Score the header-line cache against *captured* browser traffic.

Takes the JSON `capture_browser_headers.py` writes — real header lines from a
real Chromium, grouped by the TCP connection they arrived on — and reports both
the achieved hit rate and the parse cost, so the win is measured on observed
input rather than on a hand-written model of it.

Also sweeps the **connection split**, which is the one thing a single capture
cannot settle: Chromium opens up to six connections per origin and spreads a
page across them under real latency, and a cache that lives per connection sees
fewer repeats the more the page is spread.  Re-dealing the same requests over
N connections bounds that.

    python bench/hotpath/line_cache_hitrate.py capture.json
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[0]))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

from provenance import stamp  # noqa: E402

try:
    from blackbull.server.http1_actor import _DEFAULT_LINES  # noqa: E402
except ImportError:                       # a build without the shared table
    _DEFAULT_LINES = {}

from blackbull.server.http1_actor import HTTP1Actor  # noqa: E402


def _actor() -> HTTP1Actor:
    a = HTTP1Actor.__new__(HTTP1Actor)
    a._ssl = False
    return a


def build(lines: list[str], target: str = '/x') -> bytes:
    head = [f'GET {target} HTTP/1.1'.encode()]
    head += [ln.encode('latin-1') for ln in lines]
    return b'\r\n'.join(head) + b'\r\n\r\n'


def deal(conns: list[list[list[str]]], n: int) -> list[list[list[str]]]:
    """Re-deal every captured request round-robin over *n* connections."""
    flat = [r for c in conns for r in c]
    out: list[list[list[str]]] = [[] for _ in range(n)]
    for i, req in enumerate(flat):
        out[i % n].append(req)
    return [c for c in out if c]


def hit_rate(conns) -> tuple[float, int, int, int]:
    """(combined rate, shared-table hits, per-connection hits, total lines).

    Both sources are counted: a line answered from the process-wide spec table
    was not re-validated any more than one answered from this connection's own
    cache.  They are reported apart because only the second needs warming —
    the split is what says how much of the win survives connection churn.
    """
    shared = learned = total = 0
    for conn in conns:
        actor = _actor()
        for req in conn:
            cache = getattr(actor, '_line_cache', None) or {}
            for ln in req:
                total += 1
                key = ln.encode('latin-1')
                if key in cache:
                    learned += 1
                elif key in _DEFAULT_LINES:
                    shared += 1
            actor._parse(build(req))
    rate = (shared + learned) / total if total else 0.0
    return rate, shared, learned, total


def bench(conns, iters: int) -> float:
    """µs per request over the whole capture, replayed *iters* times."""
    prebuilt = [[build(r) for r in c] for c in conns]
    n_req = sum(len(c) for c in prebuilt)
    best = []
    for _ in range(7):
        t0 = time.perf_counter_ns()
        for _ in range(iters):
            for conn in prebuilt:
                actor = _actor()
                for raw in conn:
                    actor._parse(raw)
        best.append((time.perf_counter_ns() - t0) / (iters * n_req) / 1000)
    return min(best)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('capture', type=pathlib.Path)
    ap.add_argument('--iters', type=int, default=400)
    args = ap.parse_args()

    print(stamp() + '\n')
    conns = json.loads(args.capture.read_text())
    n_req = sum(len(c) for c in conns)
    n_lines = sum(len(r) for c in conns for r in c)
    distinct = len({ln for c in conns for r in c for ln in r})

    print(f'capture: {n_req} requests, {len(conns)} connection(s), '
          f'{n_lines} header lines, {distinct} distinct')
    print(f'learn-only ceiling: {(n_lines - distinct) / n_lines:.1%} '
          f'(a purely learned cache must miss each distinct line once;'
          f' a pre-seeded line never misses, so the shared table exceeds it)\n')

    print(f'  shared spec table: {len(_DEFAULT_LINES)} entries\n')
    print('  connections   hit rate  shared learned    parse µs/req')
    for n in (1, 2, 4, 6, 12):
        if n > n_req:
            break
        dealt = deal(conns, n)
        rate, shared, learned, tot = hit_rate(dealt)
        cost = bench(dealt, args.iters)
        note = '   <- as captured' if n == len(conns) else ''
        print(f'  {n:>11}   {rate:7.1%} {shared / tot:7.1%} {learned / tot:7.1%}'
              f'   {cost:8.2f}{note}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
