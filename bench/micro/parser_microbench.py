#!/usr/bin/env python3
"""HTTP/1.1 request-head parse cost: BlackBull against the parsers peers use.

Every parser gets byte-identical request heads and runs the way its server
reuses it (one actor / parser per connection, not per request).  The heads
sweep the header count, because that is the axis real clients move along —
wrk sends one header, a browser eight, a logged-in page behind a CDN twenty —
and report the least-squares per-header slope plus the one-header fixed cost.

``EXTRA`` is a real Chrome request in send order, not synthetic filler: filler
names would miss every fast path for well-known headers and report a slope no
real client sees.  Past 32 the list would have to be invented, so it stops.

Parsers (each skipped if not installed):
  blackbull  HTTP1Actor._parse
  httptools  HttpRequestParser.feed_data — C, reused like uvicorn
  h11        a fresh Connection per call, which INCLUDES the connection
             allocation uvicorn pays once per connection: an upper bound
  sanic      the header loop from sanic/http/http1.py (decode, split, Header)

A correctness gate runs first: every parser must return the expected method,
target and header count, twice in a row (reuse must not leak state).

    python bench/micro/parser_microbench.py [--iters N] [--counts 1 8 32]
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from bench.lib.results import run_dir  # noqa: E402
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


def head(n_headers: int) -> bytes:
    lines = [b'GET ' + TARGET + b' HTTP/1.1', b'Host: 127.0.0.1:8501', *EXTRA[:n_headers - 1]]
    return b'\r\n'.join(lines) + b'\r\n\r\n'


def blackbull_parser():
    actor = HTTP1Actor.__new__(HTTP1Actor)
    actor._ssl = False
    actor._max_line = 8192

    def parse(data):
        c = actor._parse(data)
        return c.method, c.path.encode() + (b'?' + c.query_string if c.query_string else b''), \
            len(c.headers)
    return parse


def httptools_parser():
    import httptools

    class Sink:
        def on_message_begin(self):
            self.headers = []

        def on_url(self, url):
            self.url = url

        def on_header(self, name, value):
            self.headers.append((name.lower(), value))

    sink = Sink()
    parser = httptools.HttpRequestParser(sink)

    def parse(data):
        parser.feed_data(data)
        return parser.get_method().decode(), sink.url, len(sink.headers)
    return parse


def h11_parser():
    import h11

    def parse(data):
        conn = h11.Connection(h11.SERVER)
        conn.receive_data(data)
        ev = conn.next_event()
        return ev.method.decode(), ev.target, len(ev.headers)
    return parse


def sanic_parser():
    from sanic.compat import Header

    def parse(data):
        reqline, *lines = data[:-4].decode(errors='surrogateescape').split('\r\n')
        method, url, _ = reqline.split(' ')
        headers = []
        for name, value in (h.split(':', 1) for h in lines):
            headers.append((name.lower(), value.lstrip()))
        return method, url.encode(), len(Header(headers))
    return parse


PARSERS = {'blackbull': blackbull_parser, 'httptools': httptools_parser,
           'h11': h11_parser, 'sanic': sanic_parser}


def timed(fn, data: bytes, iters: int) -> float:
    """Best of seven, µs per call."""
    fn(data)
    best = []
    for _ in range(7):
        t0 = time.perf_counter_ns()
        for _ in range(iters):
            fn(data)
        best.append((time.perf_counter_ns() - t0) / iters / 1000)
    return min(best)


def slope(points: dict[int, float]) -> float:
    xs = sorted(points)
    if len(xs) < 2:
        return float('nan')
    mx, my = statistics.fmean(xs), statistics.fmean(points.values())
    return (sum((x - mx) * (points[x] - my) for x in xs)
            / sum((x - mx) ** 2 for x in xs))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    ap.add_argument('--iters', type=int, default=20000)
    ap.add_argument('--counts', type=int, nargs='+', default=[1, 2, 4, 8, 16, 32],
                    help=f'header counts, 1..{len(EXTRA) + 1}')
    args = ap.parse_args()

    parsers = {}
    for name, make in PARSERS.items():
        try:
            parsers[name] = make()
        except ImportError:
            print(f'{name}: not installed, skipped')

    print('=== correctness gate ===')
    ok = True
    for n in args.counts:
        for name, parse in parsers.items():
            want = ('GET', TARGET, n)
            got = [parse(head(n)) for _ in range(2)]
            if any(g != want for g in got):
                print(f'  FAIL {name} at {n} headers: {got[0]} then {got[1]}, want {want}')
                ok = False
    print('  gate:', 'PASS' if ok else 'FAIL')
    if not ok:
        return 1

    us: dict[str, dict[int, float]] = {name: {} for name in parsers}
    print(f'\n{"headers":>8}' + ''.join(f'{name + " µs":>14}' for name in parsers))
    for n in args.counts:
        for name, parse in parsers.items():
            us[name][n] = timed(parse, head(n), args.iters)
        print(f'{n:>8}' + ''.join(f'{us[name][n]:>14.2f}' for name in parsers))
    print('\nper-header slope (least squares): '
          + ', '.join(f'{name} {slope(p):.3f} µs' for name, p in us.items()))

    run = run_dir('parser_microbench', settings=vars(args))
    (run / 'points.json').write_text(json.dumps({
        'python': sys.version.split()[0], 'iters': args.iters,
        'us': us, 'slope_us': {name: slope(p) for name, p in us.items()},
    }, indent=2) + '\n')
    print(f'\npoints -> {run}/points.json')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
