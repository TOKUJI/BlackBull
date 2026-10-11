"""Local HTTP/1.1/HTTP/2 response ABBA using h2load and two worktrees.

python bench/response_wire.py --base PATH --head PATH > samples.jsonl
Use identical paths for the A/A control. Requires Linux CPUs 3 and 4.
"""
import argparse
import asyncio
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import select
import signal
import statistics
import subprocess
import sys

from hpack_encode import source_hashes


async def serve(root):
    sys.path.insert(0, root)
    logging.disable(logging.CRITICAL)
    from blackbull import BlackBull, Response
    from blackbull.server.server import ASGIServer
    from blackbull.client.http1 import HTTP1Client
    from blackbull.client.http2 import HTTP2Client
    app = BlackBull()

    @app.route(path='/{status:int}')
    async def respond(conn, status):
        return Response(b'x' * 128, status=status, headers=[(b'x-state', b'reused-value')])

    server = ASGIServer(app)
    server.open_socket(port=0)
    async with asyncio.TaskGroup() as group:
        group.create_task(server.run())
        await asyncio.sleep(.05)
        for client_type in (HTTP1Client, HTTP2Client):
            async with client_type('127.0.0.1', server.port) as client:
                for status in (200, 201):
                    async with asyncio.timeout(3):
                        response = await client.request('GET', f'/{status}')
                    assert response.status == status
                    assert response.body == b'x' * 128
                    assert response.headers.get(b'x-state') == b'reused-value'
        print(json.dumps({'port': server.port, 'sources': source_hashes(root)}), flush=True)


def sample(root, protocol, asgi, status, count):
    environment = dict(os.environ, PYTHONHASHSEED='0', BB_FORCE_ASGI_SCOPE=str(asgi))
    process = subprocess.Popen(['taskset', '-c', '3', sys.executable, str(Path(__file__).resolve()),
                                '--serve', root], env=environment, stdout=subprocess.PIPE,
                               stderr=subprocess.DEVNULL, text=True)
    try:
        if not select.select([process.stdout], [], [], 10)[0]:
            raise TimeoutError('server startup')
        ready = json.loads(process.stdout.readline())
        port = ready['port']
        command = ['taskset', '-c', '4', 'h2load', '-c', '16', '-t', '1', '-m',
                   '1' if protocol == 'h1' else '8']
        if protocol == 'h1':
            command.append('--h1')
        for requests in (1000, count):
            output = subprocess.check_output(command + ['-n', str(requests),
                f'http://127.0.0.1:{port}/{status}'], timeout=30, text=True)
            match = re.search(r'requests: (\d+) total, (\d+) started, (\d+) done, (\d+) succeeded, (\d+) failed, (\d+) errored, (\d+) timeout', output)
            assert match and tuple(map(int, match.groups())) == (requests,) * 4 + (0, 0, 0), output
            assert re.search(rf'status codes: {requests} 2xx, 0 3xx, 0 4xx, 0 5xx', output), output
        return {'rps': float(re.search(r'finished in .*?, ([\d.]+) req/s', output)[1]),
                'sources': ready['sources']}
    finally:
        process.terminate()
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=3)
        process.stdout.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base')
    parser.add_argument('--head')
    parser.add_argument('--serve')
    parser.add_argument('--rounds', type=int, default=3)
    parser.add_argument('--count', type=int, default=10000)
    parser.add_argument('--protocol', choices=('h1', 'h2'))
    parser.add_argument('--asgi', type=int, choices=(0, 1))
    parser.add_argument('--status', type=int, choices=(200, 201))
    args = parser.parse_args()
    if args.serve:
        asyncio.run(serve(args.serve))
        return
    if not args.base or not args.head or args.rounds < 2 or args.count < 1000:
        parser.error('base/head, rounds >= 2 and count >= 1000 are required')
    if not {3, 4} <= os.sched_getaffinity(0):
        parser.error('CPUs 3 and 4 must be available')
    roots = {arm: str(Path(root).resolve()) for arm, root in [('A', args.base), ('B', args.head)]}
    hashes = {arm: {str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
                   for path in Path(root).glob('blackbull/**/*.py')} for arm, root in roots.items()}
    print(json.dumps({'metadata': {'roots': roots, 'hashes': hashes, 'python': sys.version,
                                 'rounds': args.rounds, 'count': args.count,
                                 'child_pythonhashseed': '0'}}), flush=True)
    def terminate(signum, frame):
        raise SystemExit(128 + signum)
    signal.signal(signal.SIGTERM, terminate)
    summary = {}
    for protocol in ((args.protocol,) if args.protocol else ('h1', 'h2')):
        for asgi in ((args.asgi,) if args.asgi is not None else (0, 1)):
            for status in ((args.status,) if args.status else (200, 201)):
                ratios = []
                for round_ in range(args.rounds):
                    pair = {'A': [], 'B': []}
                    for arm in ('ABBA' if round_ % 2 == 0 else 'BAAB'):
                        result = sample(roots[arm], protocol, asgi, status, args.count)
                        pair[arm].append(result['rps'])
                        print(json.dumps({'protocol': protocol, 'asgi': asgi, 'status': status,
                                          'round': round_, 'arm': arm, **result}), flush=True)
                    ratios.append(statistics.mean(pair['B']) / statistics.mean(pair['A']))
                summary[f'{protocol}/{asgi}/{status}'] = ratios
    print(json.dumps({'complete': True, 'ratios': summary}), flush=True)


if __name__ == '__main__':
    main()
