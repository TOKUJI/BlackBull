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
one header, a browser sends eight.
"""
import statistics
import sys
import time

sys.path.insert(0, '/home/toshio/work/BlackBull')

import httptools  # noqa: E402
from blackbull.server.http1_actor import HTTP1Actor  # noqa: E402

TARGET = b'/baseline11?a=1&b=2&c=3'
EXTRA = [
    b'User-Agent: Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36',
    b'Accept: text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
    b'Accept-Encoding: gzip, deflate, br',
    b'Accept-Language: en-US,en;q=0.9',
    b'Cookie: session=8f14e45fceea167a5a36dedd4bea2543',
    b'Referer: http://127.0.0.1:8501/index.html',
    b'X-Request-Id: 3f2504e0-4f89-11d3-9a0c-0305e82c3301',
]


def request_bytes(n_extra: int) -> bytes:
    lines = [b'GET ' + TARGET + b' HTTP/1.1', b'Host: 127.0.0.1:8501']
    lines += EXTRA[:n_extra]
    return b'\r\n'.join(lines) + b'\r\n\r\n'


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

    print(f'{"headers":>8} {"BlackBull µs":>14} {"httptools µs":>14} {"ratio":>7}')
    for n in (1, 2, 4, 8):
        data = request_bytes(n - 1)
        bb, _ = bench(blackbull, data, 20000)
        ht, _ = bench(httptools_parse, data, 20000)
        print(f'{n:>8} {bb:>14.2f} {ht:>14.2f} {bb / ht:>6.1f}x')

    # Per-header slope: the term that grows with what the client sends.
    d1, d8 = request_bytes(0), request_bytes(7)
    bb1, _ = bench(blackbull, d1, 20000)
    bb8, _ = bench(blackbull, d8, 20000)
    ht1, _ = bench(httptools_parse, d1, 20000)
    ht8, _ = bench(httptools_parse, d8, 20000)
    print(f'\nper-header slope: BlackBull {(bb8 - bb1) / 7:.3f} µs, '
          f'httptools {(ht8 - ht1) / 7:.3f} µs')
    print(f'fixed cost (1 header): BlackBull {bb1:.2f} µs, httptools {ht1:.2f} µs')


if __name__ == '__main__':
    main()
