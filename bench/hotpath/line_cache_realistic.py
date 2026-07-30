#!/usr/bin/env python3
"""Does the header-line cache pay on *real* traffic, or only on benchmarks?

`parser_micro.py` replays one byte-identical request and `wrk` sends the same
bytes every time — both are 100 % hit rate, which is the cache's best case and
not what a browser does.  A real client keeps the connection alive but varies
part of its header set per request: `Accept`, `Referer` and the `Sec-Fetch-*`
family all change between a navigation, a stylesheet, a script, an image and an
XHR, while `User-Agent`, `Accept-Encoding`, `Accept-Language`, `Cookie` and the
client hints stay byte-identical.

This models that: one connection, one page load, Chrome's real header sets in
real request order.  It reports the achieved **per-line hit rate** alongside the
timing, because the cache is keyed per *line* rather than per request — which is
the property that decides whether it degrades gracefully or all-or-nothing.

    python bench/hotpath/line_cache_realistic.py [--iters N]
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

from blackbull.server.http1_actor import HTTP1Actor  # noqa: E402

HOST = b'127.0.0.1:8501'

# Byte-identical on every request a browser sends over one connection.
STABLE = [
    b'Host: ' + HOST,
    b'Connection: keep-alive',
    b'sec-ch-ua: "Chromium";v="130", "Not?A_Brand";v="99"',
    b'sec-ch-ua-mobile: ?0',
    b'sec-ch-ua-platform: "Linux"',
    b'User-Agent: Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36',
    b'Accept-Encoding: gzip, deflate, br',
    b'Accept-Language: en-US,en;q=0.9',
    b'Cookie: session=8f14e45fceea167a5a36dedd4bea2543',
    b'DNT: 1',
]

# One page load: document, two stylesheets, two scripts, four images, an XHR.
# `Accept`, `Sec-Fetch-*` and `Referer` vary by destination, exactly as Chrome
# varies them; the target varies on every request.
PAGE = [
    ('/', [b'Accept: text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
           b'Sec-Fetch-Site: none', b'Sec-Fetch-Mode: navigate',
           b'Sec-Fetch-User: ?1', b'Sec-Fetch-Dest: document',
           b'Upgrade-Insecure-Requests: 1', b'Cache-Control: max-age=0']),
    ('/static/app.css', [b'Accept: text/css,*/*;q=0.1',
                         b'Sec-Fetch-Site: same-origin', b'Sec-Fetch-Mode: no-cors',
                         b'Sec-Fetch-Dest: style', b'Referer: http://' + HOST + b'/']),
    ('/static/theme.css', [b'Accept: text/css,*/*;q=0.1',
                           b'Sec-Fetch-Site: same-origin', b'Sec-Fetch-Mode: no-cors',
                           b'Sec-Fetch-Dest: style', b'Referer: http://' + HOST + b'/']),
    ('/static/app.js', [b'Accept: */*', b'Sec-Fetch-Site: same-origin',
                        b'Sec-Fetch-Mode: no-cors', b'Sec-Fetch-Dest: script',
                        b'Referer: http://' + HOST + b'/']),
    ('/static/vendor.js', [b'Accept: */*', b'Sec-Fetch-Site: same-origin',
                           b'Sec-Fetch-Mode: no-cors', b'Sec-Fetch-Dest: script',
                           b'Referer: http://' + HOST + b'/']),
    ('/img/logo.png', [b'Accept: image/avif,image/webp,image/apng,*/*;q=0.8',
                       b'Sec-Fetch-Site: same-origin', b'Sec-Fetch-Mode: no-cors',
                       b'Sec-Fetch-Dest: image', b'Referer: http://' + HOST + b'/']),
    ('/img/hero.jpg', [b'Accept: image/avif,image/webp,image/apng,*/*;q=0.8',
                       b'Sec-Fetch-Site: same-origin', b'Sec-Fetch-Mode: no-cors',
                       b'Sec-Fetch-Dest: image', b'Referer: http://' + HOST + b'/']),
    ('/img/icon-1.svg', [b'Accept: image/avif,image/webp,image/apng,*/*;q=0.8',
                         b'Sec-Fetch-Site: same-origin', b'Sec-Fetch-Mode: no-cors',
                         b'Sec-Fetch-Dest: image', b'Referer: http://' + HOST + b'/']),
    ('/img/icon-2.svg', [b'Accept: image/avif,image/webp,image/apng,*/*;q=0.8',
                         b'Sec-Fetch-Site: same-origin', b'Sec-Fetch-Mode: no-cors',
                         b'Sec-Fetch-Dest: image', b'Referer: http://' + HOST + b'/']),
    ('/api/session', [b'Accept: application/json', b'Sec-Fetch-Site: same-origin',
                      b'Sec-Fetch-Mode: cors', b'Sec-Fetch-Dest: empty',
                      b'X-Requested-With: XMLHttpRequest',
                      b'Referer: http://' + HOST + b'/']),
]


def build(target: str, varying: list[bytes]) -> bytes:
    lines = [f'GET {target} HTTP/1.1'.encode(), *STABLE, *varying]
    return b'\r\n'.join(lines) + b'\r\n\r\n'


REQUESTS = [build(t, v) for t, v in PAGE]


def _actor() -> HTTP1Actor:
    a = HTTP1Actor.__new__(HTTP1Actor)
    a._ssl = False
    return a


def hit_rate() -> tuple[float, int, int]:
    """Fraction of header lines served from the cache over one page load.

    Measured by instrumenting the dict rather than by timing, so it is exact.
    Returns ``(rate, hits, total)``; ``(0, 0, n)`` when the build has no cache.
    """
    actor = _actor()
    actor._parse(REQUESTS[0])          # first request populates
    cache = getattr(actor, '_line_cache', None)

    hits = total = 0
    for raw in REQUESTS[1:]:
        for line in raw.split(b'\r\n')[1:]:
            if not line:
                continue
            total += 1
            # ``cache is None`` is a build without the feature: same denominator,
            # zero hits, so the two trees report against one counting rule.
            if cache is not None and line in cache:
                hits += 1
        actor._parse(raw)
    return hits / total, hits, total


def bench(iters: int) -> float:
    """µs per request, averaged over whole page loads on one connection."""
    best = []
    for _ in range(7):
        actor = _actor()
        t0 = time.perf_counter_ns()
        for _ in range(iters):
            for raw in REQUESTS:
                actor._parse(raw)
        elapsed = time.perf_counter_ns() - t0
        best.append(elapsed / (iters * len(REQUESTS)) / 1000)
    return min(best)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--iters', type=int, default=3000)
    args = ap.parse_args()

    print(stamp() + '\n')
    rate, hits, total = hit_rate()
    per_req = bench(args.iters)
    n_hdr = statistics.fmean(len(r.split(b'\r\n')) - 2 for r in REQUESTS)

    print(f'one connection, one page load ({len(REQUESTS)} requests, '
          f'{n_hdr:.1f} headers avg)\n')
    print(f'  per-line cache hit rate  {rate:6.1%}   ({hits}/{total} lines)')
    print(f'  parse cost               {per_req:6.2f} µs/req')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
