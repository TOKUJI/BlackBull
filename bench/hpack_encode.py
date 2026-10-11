"""ABBA comparison of warmed HTTP/2 field builders in two worktrees.

Run with a shared Python environment, --base PATH --head PATH; JSON goes to stdout.
"""
import argparse
import gc
import hashlib
import json
import logging
import os
from pathlib import Path
import statistics
import subprocess
import sys
import timeit


def measure(root, count):
    root = Path(root).resolve()
    if not (root / 'blackbull/__init__.py').is_file():
        raise ValueError(f'Not a BlackBull worktree: {root}')
    root = str(root)
    sys.path.insert(0, root)
    from blackbull.protocol.frame import FrameFactory
    from blackbull.protocol.frame_types import FrameTypes, HeaderFrameFlags, PseudoHeaders
    from blackbull.server.sender import build_response_headers, build_trailers

    sources = {}
    for name, module in tuple(sys.modules.items()):
        if name == 'blackbull' or name.startswith('blackbull.'):
            source = Path(module.__file__).resolve()
            if not source.is_relative_to(root):
                raise RuntimeError(f'Imported {name} outside {root}')
            sources[str(source.relative_to(root))] = hashlib.sha256(source.read_bytes()).hexdigest()

    logging.disable(logging.CRITICAL)
    fields = [(b'x-state', b'reused-value'), (b'date', b'Sun, 11 Oct 2026 00:00:00 GMT')]
    factory = FrameFactory()
    request = {PseudoHeaders.METHOD: 'GET', PseudoHeaders.SCHEME: 'https',
               PseudoHeaders.PATH: '/', PseudoHeaders.AUTHORITY: 'example.com'}

    def frame(pseudo):
        value = factory.create(FrameTypes.HEADERS, HeaderFrameFlags.END_HEADERS, 1)
        value.pseudo_headers = pseudo
        value.headers = list(fields)
        return value.save()

    cases = {
        'response': lambda: build_response_headers(factory.encoder, 1, 200, fields, end_stream=True),
        'nonstatic': lambda: build_response_headers(factory.encoder, 1, 201, fields, end_stream=True),
        'trailers': lambda: build_trailers(factory.encoder, 1, fields),
        'headers': lambda: frame({PseudoHeaders.STATUS: '200'}),
        'request': lambda: frame(request),
        'push': lambda: factory.push_promise(1, 2, request, fields).save(),
    }
    results = {}
    for name, call in cases.items():
        for _ in range(2000):
            call()
        gc.collect()
        results[name] = timeit.timeit(call, number=count) * 1e9 / count
    return {'ns_per_call': results, 'source_sha256': sources}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base')
    parser.add_argument('--head')
    parser.add_argument('--worker')
    parser.add_argument('--count', type=int, default=100000)
    parser.add_argument('--rounds', type=int, default=6)
    args = parser.parse_args()
    if args.count < 1 or args.rounds < 1:
        parser.error('count and rounds must be positive')
    if args.worker:
        print(json.dumps(measure(args.worker, args.count)))
        return
    if not args.base or not args.head:
        parser.error('--base and --head are required')
    os.sched_setaffinity(0, {min(os.sched_getaffinity(0))})
    roots = {'A': str(Path(args.base).resolve()), 'B': str(Path(args.head).resolve())}
    samples = []
    ratios = {}
    for round_ in range(args.rounds):
        pair = {'A': [], 'B': []}
        for arm in ('ABBA' if round_ % 2 == 0 else 'BAAB'):
            result = json.loads(subprocess.check_output([
                sys.executable, str(Path(__file__).resolve()), '--worker', roots[arm],
                '--count', str(args.count)], timeout=120, cwd=roots[arm]))
            values = result['ns_per_call']
            pair[arm].append(values)
            samples.append({'round': round_, 'arm': arm, **result})
        for name in values:
            ratio = statistics.mean(x[name] for x in pair['B']) / statistics.mean(x[name] for x in pair['A'])
            ratios.setdefault(name, []).append(ratio)
    print(json.dumps({'python': sys.version, 'roots': roots, 'count': args.count,
                      'rounds': args.rounds, 'warmup': 2000, 'cpus': sorted(os.sched_getaffinity(0)),
                      'samples': samples, 'median_head_base': {
                          name: statistics.median(values) for name, values in ratios.items()}}, indent=2))


if __name__ == '__main__':
    main()
