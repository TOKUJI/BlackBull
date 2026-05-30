"""Measure per-layer overhead of the global middleware chain.

Spawns a BlackBull server with N no-op middleware in front of a tiny
``/ping`` handler, runs an h2load pass, and prints mean request latency
+ req/s.  Run N=0, 1, 4, 10, 20, 40 and watch how the numbers scale.

Run from the repo root::

    BB_UVLOOP=1 BB_ACCESS_LOG=0 python bench/middleware_stack.py

The bench server is HTTPS so we can speak HTTP/2 — the same wire
BlackBull-in-production uses.  Each measurement uses its own server
process so the cold-start chain build doesn't pollute later numbers.
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from multiprocessing import Process

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from blackbull import BlackBull


def _make_app(n_middleware: int) -> BlackBull:
    """Build an app whose only route is /ping, behind *n* no-op middleware."""
    app = BlackBull()

    @app.route(path='/ping')
    async def ping(scope, receive, send):
        await send({'type': 'http.response.start', 'status': 200,
                    'headers': [(b'content-type', b'text/plain')]})
        await send({'type': 'http.response.body', 'body': b'pong'})

    for i in range(n_middleware):
        async def noop(scope, receive, send, call_next, _i=i):
            await call_next(scope, receive, send)
        app.use(noop)

    return app


def _run_server(n_middleware: int, port: int, cert: str, key: str) -> None:
    app = _make_app(n_middleware)
    app.run(port=port, certfile=cert, keyfile=key)


def _h2load(url: str, n: int, c: int, m: int) -> dict:
    """Run h2load and parse mean latency + req/s out of the summary line."""
    out = subprocess.run(
        ['h2load', '-n', str(n), '-c', str(c), '-m', str(m), url],
        capture_output=True, text=True, timeout=120,
    )
    text = out.stdout + out.stderr
    finished = None
    mean_ms = None
    for line in text.splitlines():
        line = line.strip()
        if line.startswith('finished in '):
            # e.g. "finished in 1.23s, 4567.89 req/s, 12.3MB/s"
            finished = line
        elif line.startswith('time for request:'):
            # min max mean sd +/- sd
            parts = line.split()
            # parts[5] is the mean (e.g. "12.34ms" or "1.23s")
            mean = parts[5]
            if mean.endswith('ms'):
                mean_ms = float(mean[:-2])
            elif mean.endswith('s'):
                mean_ms = float(mean[:-1]) * 1000.0
            elif mean.endswith('us'):
                mean_ms = float(mean[:-2]) / 1000.0
    return {'finished': finished, 'mean_ms': mean_ms}


def _bench(n_middleware: int, port: int, cert: str, key: str,
           reqs: int, conns: int, streams: int) -> dict:
    """Spin up a one-shot server with n_middleware layers, run h2load, kill."""
    p = Process(target=_run_server, args=(n_middleware, port, cert, key))
    p.start()
    try:
        # wait for port to come up
        import socket
        for _ in range(50):
            try:
                s = socket.create_connection(('127.0.0.1', port), timeout=0.2)
                s.close()
                break
            except OSError:
                time.sleep(0.2)
        else:
            return {'n': n_middleware, 'error': 'server did not bind'}

        # warmup
        _h2load(f'https://127.0.0.1:{port}/ping', reqs // 10, conns, streams)
        result = _h2load(f'https://127.0.0.1:{port}/ping', reqs, conns, streams)
        result['n'] = n_middleware
        return result
    finally:
        p.terminate()
        p.join(timeout=5)
        if p.is_alive():
            p.kill()
            p.join()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=8443)
    parser.add_argument('--cert', default='cert.pem')
    parser.add_argument('--key', default='key.pem')
    parser.add_argument('-n', type=int, default=50000,
                        help='requests per measurement (default 50000)')
    parser.add_argument('-c', type=int, default=50,
                        help='concurrent connections (default 50)')
    parser.add_argument('-m', type=int, default=10,
                        help='streams per connection (default 10)')
    parser.add_argument('--layers', type=str, default='0,1,4,10,20,40',
                        help='comma-separated list of middleware counts')
    args = parser.parse_args()

    layer_counts = [int(x) for x in args.layers.split(',')]
    print(f'h2load -n {args.n} -c {args.c} -m {args.m} https://127.0.0.1:{args.port}/ping')
    print()
    print(f'{"layers":>7}  {"mean lat (ms)":>14}  {"finished":<60}')
    print(f'{"":>7}  {"":>14}  {"":<60}')
    baseline_mean: float | None = None
    for n in layer_counts:
        r = _bench(n, args.port, args.cert, args.key,
                   args.n, args.c, args.m)
        mean = r.get('mean_ms')
        finished = (r.get('finished') or '').replace('finished ', '')
        if mean is None:
            print(f'{n:>7}  {"(error)":>14}  {finished}')
            continue
        delta = ''
        if baseline_mean is None:
            baseline_mean = mean
        else:
            d = mean - baseline_mean
            per_layer = d / n if n > 0 else 0
            delta = f'   Δ={d:+.3f} ({per_layer:+.4f}/layer)'
        print(f'{n:>7}  {mean:>11.3f} ms  {finished}{delta}')


if __name__ == '__main__':
    main()
