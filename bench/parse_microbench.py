"""HTTP/1.1 `_parse` microbenchmark — Sprint 77 (zero-copy P3 go/no-go).

`zero-copy-native-interface.md` phase 2 gates the single-normalized-buffer
parser on a **+10 % RPS** EC2 result, and notes the prototype "is expected
to be hard to pass" because profiling puts `_parse` at only ~2.1 %
self-time on B1.  Before committing 3–5 sessions to the buffer rewrite,
this measures the *absolute* per-request parse cost and the slice the
rewrite actually targets — the header-copy work (`.split`, per-line
`bytes` slicing, `.lower()`, `.strip()`) that a memoryview design would
remove.  If that slice is a small fraction of a request's total CPU, the
+10 % gate is unreachable and the native `Connection` seam parks on
capability grounds (per the proposal), decided *before* the build.

Run:  python bench/parse_microbench.py [--repeat 7] [--number 50000]
"""
import argparse
import timeit

from blackbull.server.http1_actor import HTTP1Actor


def _make_handler() -> HTTP1Actor:
    # reader/writer/app are unused by _parse; pass trivial stand-ins.
    return HTTP1Actor(
        reader=object(), writer=object(), app=None, aggregator=None,
    )


REQUESTS = {
    'tiny_get': (
        b'GET /plaintext HTTP/1.1\r\n'
        b'Host: localhost\r\n'
        b'\r\n'
    ),
    'typical_get': (
        b'GET /api/resource/12345?page=2&sort=desc HTTP/1.1\r\n'
        b'Host: example.com\r\n'
        b'User-Agent: Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36\r\n'
        b'Accept: text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8\r\n'
        b'Accept-Encoding: gzip, deflate, br\r\n'
        b'Accept-Language: en-US,en;q=0.9\r\n'
        b'Connection: keep-alive\r\n'
        b'\r\n'
    ),
    'heavy_headers': (
        b'GET /api/resource/12345 HTTP/1.1\r\n'
        b'Host: example.com\r\n'
        b'User-Agent: Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36\r\n'
        b'Accept: text/html,application/xhtml+xml,application/xml;q=0.9\r\n'
        b'Accept-Encoding: gzip, deflate, br\r\n'
        b'Accept-Language: en-US,en;q=0.9\r\n'
        b'Cookie: session=abc123def456; theme=dark; sidebar=collapsed; tz=UTC\r\n'
        b'Authorization: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.payload\r\n'
        b'X-Request-Id: 7f3e9c1a-2b4d-4e6f-8a0b-1c2d3e4f5a6b\r\n'
        b'X-Forwarded-For: 203.0.113.7\r\n'
        b'Referer: https://example.com/previous/page?with=query\r\n'
        b'Connection: keep-alive\r\n'
        b'\r\n'
    ),
}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument('--number', type=int, default=50000)
    ap.add_argument('--repeat', type=int, default=7)
    args = ap.parse_args()

    h = _make_handler()
    # Sanity: each request parses without raising.
    for name, data in REQUESTS.items():
        scope = h._parse(data)
        assert scope['type'] == 'http', name

    print(f'{"request":<16} {"bytes":>6} {"headers":>8} {"_parse ns":>12}')
    for name, data in REQUESTS.items():
        n_headers = data.count(b'\r\n') - 1
        best = min(timeit.repeat(
            lambda d=data: h._parse(d), number=args.number, repeat=args.repeat))
        ns = best / args.number * 1e9
        print(f'{name:<16} {len(data):>6} {n_headers:>8} {ns:>12.0f}')

    print('\nContext: profiling puts _parse at ~2.1% self-time on B1 '
          '(bench/CHARACTERIZATION.md).\nThe zero-copy target is the '
          'header-copy slice *within* these numbers, not the whole cost.')


if __name__ == '__main__':
    main()
