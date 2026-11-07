"""Measure per-layer overhead of the global middleware chain.

Spawns a BlackBull server with N no-op middleware in front of a tiny
``/ping`` handler, runs an h2load pass, and prints mean request latency
+ req/s.  Run N=0, 1, 4, 10, 20, 40 and watch how the numbers scale.

Run from the repo root::

    BB_UVLOOP=1 BB_ACCESS_LOG=0 python bench/micro/middleware_stack.py

The bench server is HTTPS so we can speak HTTP/2 — the same wire
BlackBull-in-production uses.  Each measurement uses its own server
process so the cold-start chain build doesn't pollute later numbers.
"""
from __future__ import annotations

import argparse
import asyncio
from collections import Counter
import gc
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from multiprocessing import Process

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..')))

def _make_app(n_middleware: int):
    """Build an app whose only route is /ping, behind *n* no-op middleware."""
    from blackbull import BlackBull
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


def _compression_case(case):
    import gzip
    from blackbull.connection import Connection
    from blackbull.middleware import compression
    from blackbull.native import NativeResponse

    body = b'0123456789' * (1 if case.startswith('range') else 50)
    status = 206 if case.startswith('range206') else 200
    fields = [(b'content-type', b'image/png' if case == 'binary' else
               b'application/grpc' if case == 'grpc-trailers' else b'text/plain')]
    if case in ('range206', 'range200'):
        fields.append((b'content-range', b'bytes 0-9/1000'))
    if case == 'preencoded':
        body = gzip.compress(body)
        fields.append((b'content-encoding', b'gzip'))
    fields.append((b'content-length', str(len(body)).encode()))
    conn = Connection.from_scope({'type': 'http', 'method': 'GET', 'path': '/',
        'headers': [(b'accept-encoding', b'identity' if case == 'identity' else b'gzip')]})
    middleware = compression.Compression(min_size=1)
    events = []

    async def send(event):
        events.append(event)

    async def next_(conn, receive, send):
        if case == 'gzip-split':
            await send(NativeResponse(status=status, header=fields))
            await send(NativeResponse(body=body))
        elif case == 'grpc-trailers':
            await send(NativeResponse.with_trailers(status, fields, body,
                                                   [(b'grpc-status', b'0')]))
        else:
            await send(NativeResponse(status=status, header=fields, body=body))

    async def operation():
        events.clear()
        await middleware(conn, None, send, next_)

    def check():
        assert len(events) == 1
        event = events[0]
        head = next(e for e in event.to_asgi() if e['type'] == 'http.response.start')
        coding = dict(head['headers']).get(b'content-encoding')
        assert head['status'] == status
        expected = gzip.decompress(body) if case == 'preencoded' else body
        assert (gzip.decompress(event.body) if coding == b'gzip' else event.body) == expected
        if case.startswith('gzip-'):
            assert coding == b'gzip'
        elif case == 'grpc-trailers':
            assert coding is None and event.trailers.get(b'grpc-status') == b'0'
        return {'encoding': coding.decode() if coding else None}

    return operation, check


def _response_case(case):
    from blackbull.response import (Response, JSONResponse, RedirectResponse,
                                    StreamingResponse, EventSourceResponse)
    events = []
    extra = [(b'x-extra', b'v')]
    explicit = [(b'content-type', b'application/custom')]
    many = [(f'x-{i}'.encode(), b'v') for i in range(12)]
    strings = [('X-Extra', 'v')]
    many_strings = [(f'X-{i}', 'v') for i in range(12)]

    async def chunks():
        yield b'hello'

    factory = {
        'response-default': lambda: Response(b'hello'),
        'response-extra': lambda: Response(b'hello', headers=extra),
        'response-explicit': lambda: Response(b'hello', headers=explicit),
        'response-many': lambda: Response(b'hello', headers=many),
        'response-strings': lambda: Response(b'hello', headers=strings),
        'response-many-strings': lambda: Response(b'hello', headers=many_strings),
        'response-explicit-strings': lambda: Response(b'hello', headers={'Content-Type': 'application/custom'}),
        'json-default': lambda: JSONResponse({'ok': True}),
        'json-extra': lambda: JSONResponse({'ok': True}, headers=extra),
        'json-explicit': lambda: JSONResponse({'ok': True}, headers=explicit),
        'redirect-default': lambda: RedirectResponse('/target'),
        'redirect-extra': lambda: RedirectResponse('/target', headers=extra),
        'redirect-explicit': lambda: RedirectResponse('/target', headers=explicit),
        'stream-default': lambda: StreamingResponse(chunks()),
        'stream-extra': lambda: StreamingResponse(chunks(), headers=extra),
        'stream-explicit': lambda: StreamingResponse(chunks(), headers=explicit),
        'sse-default': lambda: EventSourceResponse(chunks()),
        'sse-extra': lambda: EventSourceResponse(chunks(), headers=extra),
        'sse-explicit': lambda: EventSourceResponse(chunks(), headers=explicit),
        'sse-many': lambda: EventSourceResponse(chunks(), headers=many),
    }
    if case == 'response-serialize':
        cached = Response(b'hello')
        make = lambda: cached
    else:
        make = factory[case]

    async def send(event):
        events.append(event)

    if case.startswith(('stream-', 'sse-')):
        async def operation():
            events.clear()
            await make()(None, None, send)
    else:
        async def operation():
            events.clear()
            events.append(make().to_native())

    def check():
        expanded = [e for event in events for e in event.to_asgi()]
        head = next(e for e in expanded if e['type'] == 'http.response.start')
        assert head['status'] == (302 if case.startswith('redirect-') else 200)
        content_types = [v for k, v in head['headers'] if k == b'content-type']
        assert content_types
        if '-explicit' in case:
            assert content_types[-1] == b'application/custom'
        else:
            assert len(content_types) == 1
        expected = (b'{"ok": true}' if case.startswith('json-') else
                    b'' if case.startswith('redirect-') else
                    b'data: hello\n\n' if case.startswith('sse-') else b'hello')
        assert b''.join(e.get('body', b'') for e in expanded) == expected
        return {'content_types': [v.decode() for v in content_types]}

    return operation, check


async def _probe(root: str, count: int, cpu: int, gc_mode: str,
                 instructions: bool, profile: str) -> dict:
    root = str(Path(root).resolve())
    sys.path.insert(0, root)
    from blackbull import response
    from blackbull.middleware import compression
    source = Path((response if profile == 'responses' else compression).__file__).resolve()
    relative = 'response.py' if profile == 'responses' else 'middleware/compression.py'
    assert source == Path(root) / 'blackbull' / relative
    os.sched_setaffinity(0, {cpu})
    (gc.enable if gc_mode == 'on' else gc.disable)()
    counts, calls = Counter(), Counter()
    monitor = getattr(sys, 'monitoring', None)
    if instructions:
        if monitor is None:
            raise RuntimeError('Instruction counting needs Python 3.12+')
        monitor.use_tool_id(monitor.PROFILER_ID, 'middleware-stack')
        prefix = str(Path(root) / 'blackbull') + os.sep

        def instruction(code, offset):
            if code.co_filename.startswith(prefix):
                counts[code.co_qualname] += 1

        def call(code, offset):
            if code.co_filename.startswith(prefix):
                calls[code.co_qualname] += 1

        monitor.register_callback(monitor.PROFILER_ID, monitor.events.INSTRUCTION, instruction)
        monitor.register_callback(monitor.PROFILER_ID, monitor.events.PY_START, call)

    metrics = {}
    cases = (('response-default', 'response-extra', 'response-explicit', 'response-many',
              'response-strings', 'response-many-strings', 'response-explicit-strings',
              'json-default', 'json-extra', 'json-explicit', 'redirect-default',
              'redirect-extra', 'redirect-explicit', 'stream-default', 'stream-extra',
              'stream-explicit', 'sse-default', 'sse-extra', 'sse-explicit', 'sse-many',
              'response-serialize') if profile == 'responses' else
             ('gzip-complete', 'gzip-split', 'identity', 'binary', 'preencoded',
              'range206', 'range206-no-header', 'range200', 'grpc-trailers'))
    for case in cases:
        operation, check = (_response_case if profile == 'responses' else _compression_case)(case)
        for _ in range(100):
            await operation()
        counts.clear()
        calls.clear()
        if instructions:
            monitor.set_events(monitor.PROFILER_ID,
                               monitor.events.INSTRUCTION | monitor.events.PY_START)
        wall, cpu_start = time.perf_counter_ns(), time.process_time_ns()
        for _ in range(count):
            await operation()
        cpu_end, wall_end = time.process_time_ns(), time.perf_counter_ns()
        if instructions:
            monitor.set_events(monitor.PROFILER_ID, 0)
        metrics[case] = {'wall_us': (wall_end - wall) / count / 1000,
                         'cpu_us': (cpu_end - cpu_start) / count / 1000,
                         **check(),
                         'instructions': {k: v / count for k, v in sorted(counts.items())},
                         'python_calls': {k: v / count for k, v in sorted(calls.items())}}
    if instructions:
        monitor.free_tool_id(monitor.PROFILER_ID)
    return {'metrics': metrics, 'source': str(source),
            'sha256': hashlib.sha256(source.read_bytes()).hexdigest(),
            'affinity': sorted(os.sched_getaffinity(0)), 'gc': gc_mode}


def _compare(args) -> None:
    from bench.lib.results import run_dir
    from bench.micro.testing_client import bytecode
    run = run_dir('middleware_stack', vars(args))
    roots = {'base': str(Path(args.base).resolve()), 'treat': str(Path(args.head).resolve())}
    relative = 'blackbull/response.py' if args.responses else 'blackbull/middleware/compression.py'
    states = {arm: bytecode(root, (relative,))
              for arm, root in roots.items()}
    (run / 'bytecode.json').write_text(json.dumps(states, indent=2))

    def child(root, gc_mode, instructions=False):
        command = [sys.executable, __file__,
                   '--response-worker' if args.responses else '--compression-worker', root,
                   '--count', str(16 if instructions else args.count),
                   '--cpu', str(args.cpu), '--gc', gc_mode]
        if instructions:
            command.append('--instructions')
        return json.loads(subprocess.check_output(command, text=True, timeout=60,
                          env={**os.environ, 'PYTHONHASHSEED': '0'}))

    rows = []
    for gc_mode in (('on', 'off') if args.gc == 'both' else (args.gc,)):
        for phase, rounds in (('null', args.aa_rounds), ('real', args.rounds)):
            for rnd in range(rounds):
                for arm in (('base', 'treat', 'treat', 'base') if rnd % 2 == 0
                            else ('treat', 'base', 'base', 'treat')):
                    source = 'base' if phase == 'null' else arm
                    rows.append({'phase': phase, 'round': rnd, 'arm': arm, 'source_arm': source,
                                 **child(roots[source], gc_mode)})
                    (run / 'samples.json').write_text(json.dumps(rows, indent=2))
            print(f'{gc_mode}/{phase}: {rounds} rounds complete', flush=True)
    (run / 'instructions.json').write_text(json.dumps(
        {arm: child(root, 'off', True) for arm, root in roots.items()}, indent=2))
    (run / 'COMPLETE').write_text('exit_code=0\n')
    print(run)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--compression', action='store_true', help='Compression middleware A/A and ABBA')
    parser.add_argument('--responses', action='store_true', help='Response helper A/A and ABBA')
    parser.add_argument('--base')
    parser.add_argument('--head')
    parser.add_argument('--rounds', type=int, default=16)
    parser.add_argument('--aa-rounds', type=int, default=8)
    parser.add_argument('--count', type=int, default=1000)
    parser.add_argument('--cpu', type=int, default=min(os.sched_getaffinity(0)))
    parser.add_argument('--gc', choices=('on', 'off', 'both'), default='both')
    parser.add_argument('--compression-worker', help=argparse.SUPPRESS)
    parser.add_argument('--response-worker', help=argparse.SUPPRESS)
    parser.add_argument('--instructions', action='store_true', help=argparse.SUPPRESS)
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
    if args.compression_worker or args.response_worker:
        if args.gc == 'both':
            parser.error('worker requires one GC mode')
        print(json.dumps(asyncio.run(_probe(
            args.response_worker or args.compression_worker, args.count, args.cpu,
            args.gc, args.instructions, 'responses' if args.response_worker else 'compression'))))
        return
    if args.compression or args.responses:
        if sys.version_info < (3, 12):
            parser.error('compression diagnostics need Python 3.12+')
        if not args.base or not args.head or min(args.rounds, args.aa_rounds, args.count) < 1:
            parser.error('compression requires base/head and positive rounds/count')
        _compare(args)
        return

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
