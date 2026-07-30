#!/usr/bin/env python3
"""Head-to-head on the one thing the two stacks do not share: the parser.

BlackBull owns every byte of HTTP/1.1 in Python (an architectural rule, not
an oversight); uvicorn delegates to httptools, a C wrapper around the
Node.js parser.  Everything else in a request — routing, the handler, the
response write — is Python on both sides, so if the frameworks differ in
per-request cost this is the first place to look, and it is the one place
where the difference is structural rather than incidental.

Both are fed byte-identical requests.  The comparison is deliberately
per-header, because that is the axis a real client moves along: wrk sends
one header, a browser sends eight, and a logged-in page behind a CDN can
send twenty.

``EXTRA`` is a real Chrome request in real send order, not synthetic
filler.  That matters for anything that special-cases well-known names:
filler like ``X-Filler-07`` would miss every such fast path and report a
slope no real client would see.  Past 32 the list would have to be
invented, which is why the sweep stops there.
"""
import argparse
import json
import pathlib
import statistics
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

import httptools  # noqa: E402
from blackbull.server.http1_actor import HTTP1Actor  # noqa: E402

TARGET = b'/baseline11?a=1&b=2&c=3'
EXTRA = [
    b'Connection: keep-alive',
    b'Cache-Control: max-age=0',
    b'sec-ch-ua: "Chromium";v="130", "Not?A_Brand";v="99"',
    b'sec-ch-ua-mobile: ?0',
    b'sec-ch-ua-platform: "Linux"',
    b'Upgrade-Insecure-Requests: 1',
    b'User-Agent: Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36',
    b'Accept: text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
    b'Sec-Fetch-Site: same-origin',
    b'Sec-Fetch-Mode: navigate',
    b'Sec-Fetch-User: ?1',
    b'Sec-Fetch-Dest: document',
    b'Accept-Encoding: gzip, deflate, br',
    b'Accept-Language: en-US,en;q=0.9',
    b'Cookie: session=8f14e45fceea167a5a36dedd4bea2543',
    b'Referer: http://127.0.0.1:8501/index.html',
    b'Origin: http://127.0.0.1:8501',
    b'DNT: 1',
    b'Pragma: no-cache',
    b'If-None-Match: W/"3f2504e0-4f89"',
    b'If-Modified-Since: Wed, 29 Jul 2026 10:00:00 GMT',
    b'TE: trailers',
    b'X-Requested-With: XMLHttpRequest',
    b'Authorization: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9',
    b'X-Forwarded-For: 203.0.113.7',
    b'X-Forwarded-Proto: http',
    b'X-Forwarded-Host: example.test',
    b'X-Real-Ip: 203.0.113.7',
    b'X-Request-Id: 3f2504e0-4f89-11d3-9a0c-0305e82c3301',
    b'X-Correlation-Id: 9b2c1f0e-77aa-4c31-b5c6-0d1e2f3a4b5c',
    b'Priority: u=0, i',
]
COUNTS = (1, 2, 4, 8, 16, 32)


def request_bytes(n_extra: int) -> bytes:
    if n_extra > len(EXTRA):
        raise ValueError(f'only {len(EXTRA)} real headers available')
    lines = [b'GET ' + TARGET + b' HTTP/1.1', b'Host: 127.0.0.1:8501']
    lines += EXTRA[:n_extra]
    return b'\r\n'.join(lines) + b'\r\n\r\n'


def slope(points: dict[int, float]) -> float:
    """Least-squares µs-per-header over every measured count.

    A 1→8 endpoint difference is one subtraction of two noisy numbers; the
    regression uses all six and is what the sweep is for.
    """
    xs = sorted(points)
    mx = statistics.fmean(xs)
    my = statistics.fmean(points[x] for x in xs)
    num = sum((x - mx) * (points[x] - my) for x in xs)
    den = sum((x - mx) ** 2 for x in xs)
    return num / den


class _Sink:
    """httptools needs a callback target; keep it as cheap as uvicorn's."""

    def __init__(self):
        self.headers = []

    def on_message_begin(self):
        # uvicorn resets per message on a reused parser; without this the
        # list grows across iterations and the benchmark measures realloc.
        self.headers = []

    def on_url(self, url):
        self.url = url

    def on_header(self, name, value):
        self.headers.append((name.lower(), value))

    def on_headers_complete(self):
        pass

    def on_body(self, body):
        pass

    def on_message_complete(self):
        pass


def bench(fn, data, iters):
    fn(data)  # warm the branch predictors and any lazily-built state
    best = []
    for _ in range(7):
        t0 = time.perf_counter_ns()
        for _ in range(iters):
            fn(data)
        best.append((time.perf_counter_ns() - t0) / iters / 1000)  # µs
    return min(best), statistics.median(best)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--iters', type=int, default=20000)
    ap.add_argument('--json', type=pathlib.Path,
                    help='also write the raw points here, for before/after diffing')
    ap.add_argument('--label', default='', help='tag recorded in the JSON')
    args = ap.parse_args()

    actor = HTTP1Actor.__new__(HTTP1Actor)
    actor._ssl = False

    def blackbull(data):
        return actor._parse(data)

    # One parser for the whole run, as uvicorn keeps one per connection —
    # constructing a parser per request would be a cost uvicorn never pays.
    sink = _Sink()
    parser = httptools.HttpRequestParser(sink)

    def httptools_parse(data):
        parser.feed_data(data)

    bb_pts: dict[int, float] = {}
    ht_pts: dict[int, float] = {}
    print(f'{"headers":>8} {"BlackBull µs":>14} {"httptools µs":>14} {"ratio":>7}')
    for n in COUNTS:
        data = request_bytes(n - 1)
        bb_pts[n], _ = bench(blackbull, data, args.iters)
        ht_pts[n], _ = bench(httptools_parse, data, args.iters)
        print(f'{n:>8} {bb_pts[n]:>14.2f} {ht_pts[n]:>14.2f} '
              f'{bb_pts[n] / ht_pts[n]:>6.1f}x')

    # Two slopes.  The regression is the honest one; the 1→8 endpoint pair is
    # kept because every number recorded before this sweep existed was 1→8,
    # and a target quoted against that basis has to be checked against it.
    bb_slope, ht_slope = slope(bb_pts), slope(ht_pts)
    bb_18, ht_18 = (bb_pts[8] - bb_pts[1]) / 7, (ht_pts[8] - ht_pts[1]) / 7
    print(f'\nper-header slope (least squares, 1..32): '
          f'BlackBull {bb_slope:.3f} µs, httptools {ht_slope:.3f} µs')
    print(f'per-header slope (1→8 endpoints):        '
          f'BlackBull {bb_18:.3f} µs, httptools {ht_18:.3f} µs')
    print(f'fixed cost (1 header): BlackBull {bb_pts[1]:.2f} µs, '
          f'httptools {ht_pts[1]:.2f} µs')

    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps({
            'label': args.label,
            'python': sys.version.split()[0],
            'iters': args.iters,
            'blackbull_us': bb_pts,
            'httptools_us': ht_pts,
            'blackbull_slope_lsq': bb_slope,
            'blackbull_slope_1_8': bb_18,
            'httptools_slope_lsq': ht_slope,
        }, indent=2, sort_keys=True) + '\n')
        print(f'\nwrote {args.json}')


if __name__ == '__main__':
    main()
