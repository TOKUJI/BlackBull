"""ABBA comparison of warmed HTTP/2 field builders in two worktrees.

Run with a shared Python environment, --base PATH --head PATH; JSON goes to stdout.
--paired isolates response builders and fast paths; other dependencies come from head.
"""
import argparse
import ast
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
import types
from itertools import chain


def source_hashes(root):
    root = Path(root).resolve()
    sources = {}
    for name, module in tuple(sys.modules.items()):
        if name == 'blackbull' or name.startswith('blackbull.'):
            source = Path(module.__file__).resolve()
            if not source.is_relative_to(root):
                raise RuntimeError(f'Imported {name} outside {root}')
            sources[str(source.relative_to(root))] = hashlib.sha256(source.read_bytes()).hexdigest()
    return sources


def measure(root, count):
    root = Path(root).resolve()
    if not (root / 'blackbull/__init__.py').is_file():
        raise ValueError(f'Not a BlackBull worktree: {root}')
    root = str(root)
    sys.path.insert(0, root)
    from blackbull.protocol.frame import FrameFactory
    from blackbull.protocol.frame_types import FrameTypes, HeaderFrameFlags, PseudoHeaders
    from blackbull.server.sender import build_response_headers, build_trailers

    sources = source_hashes(root)

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


def paired(roots, count, rounds):
    sys.path.insert(0, roots['B'])
    from hpack import Encoder
    from blackbull.server import sender
    from blackbull.protocol.frame_types import PseudoHeaders
    if not Path(sender.__file__).resolve().is_relative_to(roots['B']):
        raise RuntimeError('Imported sender outside the head worktree')
    logging.disable(logging.CRITICAL)
    functions, hashes = {}, {}
    for arm, root in roots.items():
        root = Path(root)
        source = (root / 'blackbull/protocol/hpack_fastpath.py').read_bytes()
        module = types.ModuleType('paired_hpack')
        exec(compile(source, str(root / 'blackbull/protocol/hpack_fastpath.py'), 'exec'), vars(module))
        code = (root / 'blackbull/server/sender.py').read_bytes()
        nodes = [node for node in ast.parse(code).body
                 if isinstance(node, ast.FunctionDef) and node.name == 'build_response_headers']
        if len(nodes) != 1:
            raise ValueError('Expected one response builder')
        namespace = dict(vars(sender), hpack_fastpath=module, chain=chain, PseudoHeaders=PseudoHeaders)
        exec(compile(ast.Module(body=nodes, type_ignores=[]), str(root / 'blackbull/server/sender.py'), 'exec'), namespace)
        functions[arm] = namespace['build_response_headers']
        hashes[arm] = {'sender': hashlib.sha256(code).hexdigest(),
                       'hpack': hashlib.sha256(source).hexdigest()}
    fields = [(b'date', b'Sun, 11 Oct 2026 00:00:00 GMT'), (b'x-state', b'reused-value')]
    samples, ratios = [], {}
    for status in (200, 201):
        calls = {}
        for arm in 'AB':
            encoder = Encoder()
            function = functions[arm]
            calls[arm] = lambda e=encoder, f=function: f(e, 1, status, fields, end_stream=True)
            for _ in range(10000):
                calls[arm]()
        ratios[status] = []
        for round_ in range(rounds):
            pair = {'A': [], 'B': []}
            for arm in ('ABBA' if round_ % 2 == 0 else 'BAAB'):
                value = timeit.timeit(calls[arm], number=count) * 1e9 / count
                pair[arm].append(value)
                samples.append({'status': status, 'round': round_, 'arm': arm, 'ns': value})
            ratios[status].append(statistics.mean(pair['B']) / statistics.mean(pair['A']))
    return {'samples': samples, 'ratios': ratios, 'hashes': hashes, 'warmup': 10000,
            'shared_source_sha256': source_hashes(roots['B'])}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base')
    parser.add_argument('--head')
    parser.add_argument('--worker')
    parser.add_argument('--paired', action='store_true', help='Isolate response builders in one process; other dependencies come from head')
    parser.add_argument('--cpu', type=int)
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
    os.sched_setaffinity(0, {args.cpu if args.cpu is not None else min(os.sched_getaffinity(0))})
    roots = {'A': str(Path(args.base).resolve()), 'B': str(Path(args.head).resolve())}
    if args.paired:
        result = paired(roots, args.count, args.rounds)
        print(json.dumps(dict(result, python=sys.version, roots=roots, count=args.count,
                              rounds=args.rounds, cpus=sorted(os.sched_getaffinity(0)),
                              pythonhashseed=os.environ.get('PYTHONHASHSEED')), indent=2))
        return
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
                      'pythonhashseed': os.environ.get('PYTHONHASHSEED'),
                      'rounds': args.rounds, 'warmup': 2000, 'cpus': sorted(os.sched_getaffinity(0)),
                      'samples': samples, 'median_head_base': {
                          name: statistics.median(values) for name, values in ratios.items()}}, indent=2))


if __name__ == '__main__':
    main()
