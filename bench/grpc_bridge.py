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


async def measure(module, registry, shape, native, size, count, send=send):
    frame = encode_message(b'x' * size)
    conn = {'type': 'http', 'path': '/svc/' + shape,
            'headers': [(b'content-type', b'application/grpc')]}

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
    source = subprocess.check_output(
        ['git', 'show', args.baseline + ':blackbull/grpc/asgi.py'], timeout=10)
    baseline = types.ModuleType('blackbull.grpc._baseline')
    baseline.__package__ = 'blackbull.grpc'
    exec(compile(source, '<baseline>', 'exec'), baseline.__dict__)
    os.sched_setaffinity(0, {min(os.sched_getaffinity(0))})
    registry = GrpcServiceRegistry()
    registry.add_method('/svc/Unary', unary)
    registry.add_method('/svc/Collect', collect)
    print(json.dumps({'baseline': args.baseline, 'baseline_sha256': hashlib.sha256(source).hexdigest(),
                      'head_sha256': hashlib.sha256(Path(head.__file__).read_bytes()).hexdigest(),
                      'settings': vars(args), 'python': sys.version}), file=sys.stderr)
    writer = csv.writer(sys.stdout, delimiter='\t', lineterminator='\n')
    writer.writerow(['shape', 'native', 'size', 'round', 'arm', 'ns'])
    gc.disable()
    for shape in ('Unary', 'Collect'):
        for native in (False, True):
            for size in args.sizes:
                for module in (baseline, head):
                    status = None

                    async def validate(event):
                        nonlocal status
                        for item in event.to_asgi():
                            for key, value in item.get('headers', []):
                                if key == b'grpc-status':
                                    status = value

                    await measure(module, registry, shape, native, size, 1, validate)
                    if status != b'0':
                        raise RuntimeError(f'{shape}/{native}/{size}: grpc-status={status!r}')
                    await measure(module, registry, shape, native, size, args.warmup)
                for round_ in range(args.rounds):
                    for arm in ('ABBA' if round_ % 2 == 0 else 'BAAB'):
                        ns = await measure(baseline if arm == 'A' else head,
                                           registry, shape, native, size, args.calls)
                        writer.writerow([shape, native, size, round_, arm, ns])


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--baseline', required=True)
    parser.add_argument('--rounds', type=int, default=6)
    parser.add_argument('--calls', type=int, default=8000)
    parser.add_argument('--warmup', type=int, default=1000)
    parser.add_argument('--sizes', type=int, nargs='+', default=[16, 65536])
    args = parser.parse_args()
    if min(args.rounds, args.calls, args.warmup) <= 0 or min(args.sizes) < 0:
        parser.error('rounds, calls and warmup must be positive; sizes must be nonnegative')
    if max(args.sizes) > min(head.MAX_MESSAGE_SIZE, head.MAX_MESSAGE_LENGTH):
        parser.error('requested payload exceeds the current message cap')
    asyncio.run(main(args))
