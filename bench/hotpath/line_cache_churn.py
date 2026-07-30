#!/usr/bin/env python3
"""Where does the header-line cache break even, in requests per connection?

The cache is per connection, so the first request on every connection pays for
it and gets nothing back: every lookup misses and every line is admitted into a
dict that is discarded when the connection closes.  Only from the second
request does any of that work get repaid.  A workload that reconnects often
therefore pays the setup repeatedly and collects less of the benefit — and
HttpArena's `limited-conn` profile (upstream `n`) is exactly that shape:
**connections that close after 10 requests**.

So the honest characterisation is not one number but a break-even point.  This
replays captured browser header sets with a fresh actor every K requests and
sweeps K, against a build with no cache at all.

    python bench/hotpath/line_cache_churn.py capture.json
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

from blackbull.server.http1_actor import HTTP1Actor  # noqa: E402


def _actor() -> HTTP1Actor:
    a = HTTP1Actor.__new__(HTTP1Actor)
    a._ssl = False
    return a


def build(lines: list[str]) -> bytes:
    head = [b'GET /baseline11?a=1 HTTP/1.1']
    head += [ln.encode('latin-1') for ln in lines]
    return b'\r\n'.join(head) + b'\r\n\r\n'


def bench(requests: list[bytes], per_conn: int, iters: int) -> float:
    """µs/request when the connection is torn down every *per_conn* requests."""
    best = []
    for _ in range(7):
        t0 = time.perf_counter_ns()
        for _ in range(iters):
            actor = _actor()
            for i, raw in enumerate(requests):
                if i and i % per_conn == 0:
                    actor = _actor()          # reconnect: cache is discarded
                actor._parse(raw)
        best.append((time.perf_counter_ns() - t0)
                    / (iters * len(requests)) / 1000)
    return min(best)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('capture', type=pathlib.Path)
    ap.add_argument('--iters', type=int, default=300)
    args = ap.parse_args()

    print(stamp() + '\n')
    conns = json.loads(args.capture.read_text())
    reqs = [build(r) for c in conns for r in c]
    # Repeat the capture so the longer per-conn settings get whole connections
    # rather than one truncated one.
    reqs = reqs * 4

    print(f'{len(reqs)} requests replayed, reconnecting every K\n')
    print(f'  {"req/conn":>9}  {"µs/req":>8}')
    for k in (1, 2, 3, 5, 10, 20, len(reqs)):
        label = 'keep-alive' if k == len(reqs) else str(k)
        print(f'  {label:>9}  {bench(reqs, k, args.iters):8.2f}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
