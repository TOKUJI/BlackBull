"""Compare two gRPC bridge implementations; write samples to stdout."""
import argparse
import asyncio
import csv
import gc
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import types

import blackbull.grpc.asgi as head
from blackbull.grpc import GrpcServiceRegistry, encode_message


async def unary(request, context):
    return b'ok'


async def collect(request_iter, context):
    async for request in request_iter:
        pass
    return b'ok'


async def send(event):
    pass


async def measure(module, registry, shape, native, size, count, send=send, timeout=None):
    frame = encode_message(b'x' * size)
    conn = {'type': 'http', 'path': '/svc/' + shape,
            'headers': [(b'content-type', b'application/grpc')]}
    if timeout is not None:
        conn['headers'].append((b'grpc-timeout', timeout.encode('ascii')))

    async def receive():
        return {'type': 'http.request', 'body': frame, 'more_body': False}

    if native:
        async def next_chunk():
            nonlocal eof
            if eof:
                return None
            eof = True
            return frame
        receive.next_chunk = next_chunk
    start = time.perf_counter_ns()
    for _ in range(count):
        eof = False
        await module.serve_grpc(registry, conn, receive, send)
    return (time.perf_counter_ns() - start) / count


async def main(args):
    source = (Path(head.__file__).read_bytes() if args.null else subprocess.check_output(
        ['git', 'show', args.baseline + ':blackbull/grpc/asgi.py'], timeout=10))
    baseline = types.ModuleType('blackbull.grpc._baseline')
    baseline.__package__ = 'blackbull.grpc'
    exec(compile(source, '<baseline>', 'exec'), baseline.__dict__)
    os.sched_setaffinity(0, {min(os.sched_getaffinity(0))})
    registry = GrpcServiceRegistry()
    registry.add_method('/svc/Unary', unary)
    registry.add_method('/svc/Collect', collect)

    async def stream(request, context):
        if hasattr(request, '__aiter__'):
            async for _ in request:
                pass
        for _ in range(args.messages):
            yield b'ok'
            if args.yield_every and (_ + 1) % args.yield_every == 0:
                await asyncio.sleep(0)

    registry.add_method('/svc/Server', stream, client_streaming=False)
    registry.add_method('/svc/Bidi', stream, client_streaming=True)
    print(json.dumps({'baseline': args.baseline, 'baseline_sha256': hashlib.sha256(source).hexdigest(),
                      'head_sha256': hashlib.sha256(Path(head.__file__).read_bytes()).hexdigest(),
                      'settings': vars(args), 'python': sys.version}), file=sys.stderr)
    writer = csv.writer(sys.stdout, delimiter='\t', lineterminator='\n')
    writer.writerow(['shape', 'native', 'size', 'round', 'arm', 'ns'])
    gc.disable()
    for shape in args.shapes:
        for native in (False, True):
            for size in args.sizes:
                for module in (baseline, head):
                    status = None
                    body = bytearray()

                    async def validate(event):
                        nonlocal status
                        for item in event.to_asgi():
                            body.extend(item.get('body', b''))
                            for key, value in item.get('headers', []):
                                if key == b'grpc-status':
                                    status = value

                    await measure(module, registry, shape, native, size, 1, validate, args.timeout)
                    if status != b'0':
                        raise RuntimeError(f'{shape}/{native}/{size}: grpc-status={status!r}')
                    expected = args.messages if shape in ('Server', 'Bidi') else 1
                    if body != encode_message(b'ok') * expected:
                        raise RuntimeError(f'{shape}/{native}/{size}: response messages differ')
                    await measure(module, registry, shape, native, size, args.warmup,
                                  timeout=args.timeout)
                for round_ in range(args.rounds):
                    for arm in ('ABBA' if round_ % 2 == 0 else 'BAAB'):
                        ns = await measure(baseline if arm == 'A' else head,
                                           registry, shape, native, size, args.calls,
                                           timeout=args.timeout)
                        writer.writerow([shape, native, size, round_, arm, ns])


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--baseline', required=True)
    parser.add_argument('--null', action='store_true', help='use the current bridge for both labels')
    parser.add_argument('--timeout', help='grpc-timeout header for each RPC, e.g. 1S')
    parser.add_argument('--shapes', nargs='+', choices=('Unary', 'Collect', 'Server', 'Bidi'),
                        default=['Unary', 'Collect'])
    parser.add_argument('--messages', type=int, default=1, help='response messages per streaming RPC')
    parser.add_argument('--yield-every', type=int, default=0,
                        help='suspend the streaming producer every N messages; 0 is a synchronous burst')
    parser.add_argument('--rounds', type=int, default=6)
    parser.add_argument('--calls', type=int, default=8000)
    parser.add_argument('--warmup', type=int, default=1000)
    parser.add_argument('--sizes', type=int, nargs='+', default=[16, 65536])
    args = parser.parse_args()
    if min(args.rounds, args.calls, args.warmup, args.messages) <= 0 or min(args.sizes) < 0 or args.yield_every < 0:
        parser.error('rounds, calls, warmup and messages must be positive; sizes and yield-every nonnegative')
    if max(args.sizes) > min(head.MAX_MESSAGE_SIZE, head.MAX_MESSAGE_LENGTH):
        parser.error('requested payload exceeds the current message cap')
    asyncio.run(main(args))
