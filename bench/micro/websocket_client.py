"""WebSocket wire-client A/A, path control and ABBA against a fixed peer.

Compare --base PATH --head PATH. CPU/wall µs per operation, with separate
instruction counting. Local measurements only screen for regressions.
"""
from __future__ import annotations

import argparse
import asyncio
from collections import Counter
import compileall
import dis
import gc
import hashlib
import importlib.metadata
import json
import logging
import os
from pathlib import Path
import platform
import random
import py_compile
import select
import shutil
import ssl
import statistics as st
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'bench' / 'ab'))
FILES = ('blackbull/client/websocket.py', 'blackbull/client/websocket_h2.py',
         'blackbull/client/http2.py', 'blackbull/server/http2_ws.py',
         'blackbull/client/_connect.py', 'blackbull/server/recipient.py',
         'blackbull/_cleanup.py')


def load(root):
    sys.meta_path = [f for f in sys.meta_path if '__editable__' not in type(f).__module__
                     and '__editable__' not in getattr(f, '__name__', '')]
    sys.path.insert(0, str(root))
    logging.disable(logging.CRITICAL)


def proof(root):
    from hpack_encode import source_hashes
    return source_hashes(root)


async def peer(args):
    load(args.peer)
    from blackbull import BlackBull
    from blackbull.server.server import ASGIServer
    from blackbull.utils import Scheme
    app = BlackBull()

    @app.route(path='/http/{size:int}')
    async def http(conn, size):
        return b'x' * size

    @app.route(path='/ws', scheme=Scheme.websocket)
    async def echo(conn, receive, send):
        await receive()
        await send({'type': 'websocket.accept'})
        while (event := await receive())['type'] == 'websocket.receive':
            await send({'type': 'websocket.send', 'text': event.get('text'),
                        'bytes': event.get('bytes')})

    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(args.cert, args.key)
    context.set_alpn_protocols(['h2', 'http/1.1'])
    servers = [ASGIServer(app), ASGIServer(app, ssl_context=context)]
    for server in servers:
        server.open_socket(port=0)
    async with asyncio.TaskGroup() as group:
        for server in servers:
            group.create_task(server.run())
        await asyncio.sleep(0.05)
        print(json.dumps(dict(h1=servers[0].port, h1tls=servers[1].port,
                              h2tls=servers[1].port, source_proof=proof(args.peer))), flush=True)
        await asyncio.to_thread(sys.stdin.buffer.read, 1)
        for server in servers:
            await server.stop(drain_timeout=1.0)


async def probe(args):
    load(args.probe)
    from blackbull.client import HTTP1Client, HTTP2Client, WebSocketClient, WebSocketH2Client
    root = str(Path(args.probe).resolve()) + '/blackbull/'
    metrics, current = {}, Counter()
    collections = [0, 0, 0]
    def collection(phase, info):
        if phase == 'stop':
            collections[info['generation']] += 1
    gc.callbacks.append(collection)
    monitor = sys.monitoring if args.instructions else None
    if monitor:
        monitor.use_tool_id(monitor.PROFILER_ID, 'websocket-client')
        def instruction(code, offset):
            if code.co_filename.startswith(root):
                current[f'{code.co_filename[len(root):]}:{code.co_qualname}'] += 1
        monitor.register_callback(monitor.PROFILER_ID, monitor.events.INSTRUCTION, instruction)

    def start():
        current.clear()
        if monitor:
            monitor.set_events(monitor.PROFILER_ID, monitor.events.INSTRUCTION)
        before_gc = tuple(collections)
        return time.perf_counter_ns(), time.process_time_ns(), before_gc

    def stop(before):
        wall, cpu = time.perf_counter_ns() - before[0], time.process_time_ns() - before[1]
        result = wall, cpu, current.copy(), [a - b for a, b in zip(collections, before[2])]
        if monitor:
            monitor.set_events(monitor.PROFILER_ID, 0)
        return result

    def record(name, samples, count):
        instructions = sum((s[2] for s in samples), Counter())
        metrics[name] = dict(wall_us=sum(s[0] for s in samples) / count / 1000,
                             cpu_us=sum(s[1] for s in samples) / count / 1000,
                             instructions={k: v / count for k, v in sorted(instructions.items())},
                             gc_collections=[sum(s[3][generation] for s in samples) for generation in range(3)],
                             operations=count)

    async def exchange(session, protocol, binary=False):
        payload = b'x' * 128 if binary else 'x' * 128
        await (session.send_bytes(payload) if binary else session.send_text(payload))
        response = await session.receive()
        if protocol == 'h2tls':
            assert response == (2 if binary else 1, b'x' * 128), response
        else:
            assert response['bytes' if binary else 'text'] == payload, response

    baseline_tasks = asyncio.all_tasks()
    (gc.enable if args.gc == 'on' else gc.disable)()
    cases = args.cases.split(',')
    for protocol in args.protocols.split(','):
        context = None
        if protocol != 'h1':
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            context.check_hostname, context.verify_mode = False, ssl.CERT_NONE
            context.set_alpn_protocols(['h2' if protocol == 'h2tls' else 'http/1.1'])
        client_type = WebSocketH2Client if protocol == 'h2tls' else WebSocketClient
        def client():
            return client_type('127.0.0.1', args.ports[protocol], ssl=context)

        http_type = HTTP2Client if protocol == 'h2tls' else HTTP1Client
        for size in (128, 65536):
            name = 'http' if size == 128 else 'http-large'
            if name not in cases:
                continue
            async with http_type('127.0.0.1', args.ports[protocol], ssl=context) as http:
                count = args.messages if size == 128 else max(2, args.messages // 8)
                async def request():
                    response = await http.request('GET', f'/http/{size}')
                    assert response.status == 200 and response.body == b'x' * size
                for _ in range(2):
                    await request()
                before = start()
                for _ in range(count):
                    await request()
                record(f'{protocol}/{name}', [stop(before)], count)

        for size in (128, 65536):
            name = 'http-churn' if size == 128 else 'http-large-churn'
            if name not in cases:
                continue
            samples = []
            for i in range(2 + args.connections):
                before = start()
                async with http_type('127.0.0.1', args.ports[protocol], ssl=context) as http:
                    response = await http.request('GET', f'/http/{size}')
                    assert response.status == 200 and response.body == b'x' * size
                measured = stop(before)
                if i >= 2:
                    samples.append(measured)
                await asyncio.sleep(0)
            record(f'{protocol}/{name}', samples, args.connections)

        for binary in (False, True):
            name = 'binary' if binary else 'text'
            if name not in cases:
                continue
            async with client() as connection:
                session = await connection.connect('/ws')
                try:
                    for _ in range(10):
                        await exchange(session, protocol, binary)
                    before = start()
                    for _ in range(args.messages):
                        await exchange(session, protocol, binary)
                    record(f'{protocol}/{name}', [stop(before)], args.messages)
                    await session.close(drain_timeout=0.5)
                finally:
                    await session._recipient.shutdown()

        for case in (name for name in cases if name in ('connect', 'close', 'lifecycle')):
            samples = []
            for i in range(2 + args.connections):
                connection, session = client(), None
                try:
                    before = start() if case != 'close' else None
                    await connection.__aenter__()
                    session = await connection.connect('/ws')
                    if case == 'connect':
                        measured = stop(before)
                    await exchange(session, protocol)
                    if case == 'close':
                        before = start()
                    await session.close(drain_timeout=0.5)
                    if case == 'close':
                        measured = stop(before)
                    await connection.__aexit__(None, None, None)
                    if case == 'lifecycle':
                        measured = stop(before)
                    if i >= 2:
                        samples.append(measured)
                finally:
                    if monitor:
                        monitor.set_events(monitor.PROFILER_ID, 0)
                    if session is not None:
                        await session._recipient.shutdown()
                    await connection.__aexit__(None, None, None)
                    await asyncio.sleep(0)
            record(f'{protocol}/{case}', samples, args.connections)
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    pending = [task.get_coro().__qualname__ for task in asyncio.all_tasks() - baseline_tasks if not task.done()]
    assert not pending, pending
    if monitor:
        monitor.free_tool_id(monitor.PROFILER_ID)
    gc.callbacks.remove(collection)
    methods = {f'{cls.__name__}.{name}': getattr(cls, name)
               for cls in (WebSocketClient, WebSocketH2Client)
               for name in ('connect', '__aexit__')}
    from blackbull.client import WebSocketSession, WebSocketH2Session
    methods.update({f'{cls.__name__}.{name}': getattr(cls, name)
                    for cls in (WebSocketSession, WebSocketH2Session)
                    for name in ('send_text', 'send_bytes', 'receive', 'close')})
    compiled = {name: hashlib.sha256(json.dumps([(i.opname, i.argrepr)
                for i in dis.get_instructions(method)]).encode()).hexdigest()
                for name, method in methods.items()}
    return dict(metrics=metrics, pending_tasks=pending, source_proof=proof(args.probe),
                compiled_methods=compiled, process_id=os.getpid(),
                affinity=sorted(os.sched_getaffinity(0)), gc=args.gc, python=sys.version)


def copy_tree(source, target):
    if target.exists():
        shutil.rmtree(target)
    shutil.copytree(Path(source) / 'blackbull', target / 'blackbull',
                    ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
    assert compileall.compile_dir(target / 'blackbull', quiet=2,
        invalidation_mode=py_compile.PycInvalidationMode.CHECKED_HASH)


def report(run, rows):
    from bench.ab.report import MIN_ROUNDS, analyse
    output = {}
    text = ['# WebSocket client: base → treatment', '',
            'Local screening; all-round paired results are primary. CPU/wall are µs per operation.',
            'SD is across trial means. GC applies to client only. Peer code and CPU are fixed.',
            'Every protocol/case uses a fresh client process; cases cannot carry heap or GC state into one another.',
            'Cleanup hygiene and instruction counting run outside timing.', '',
            'connect includes TCP/TLS and upgrade; close covers session.close only; lifecycle adds one text RTT and client context exit.',
            'http/http-large reuse a connection; http-churn/http-large-churn include TCP/TLS, one response and context cleanup.',
            'AA/path time CIs must both fit ±1% to clear controls. Failed controls leave small changes unresolved.', '',
            '| GC | interval | clock | base mean ± SD | treatment mean ± SD | Δ µs [95% CI] | AA time CI % | path time CI % | controls |',
            '|---|---|---|---|---|---|---|---|---|']
    rng = random.Random(0)
    for mode in sorted({s['gc'] for s in rows}):
        selected = [s for s in rows if s['gc'] == mode]
        for name in selected[0]['metrics']:
            for clock in ('cpu_us', 'wall_us'):
                raw = run / f'{mode}-{name.replace("/", "-")}-{clock}.tsv'
                raw.write_text('phase\tround\tarm\tvalue\tproof\n' + ''.join(
                    f'{s["phase"]}\t{s["round"]}\t{s["arm"]}\t{1e6/s["metrics"][name][clock]}\t'
                    f'{hashlib.sha256(json.dumps(s["source_proof"], sort_keys=True).encode()).hexdigest()}\n'
                    for s in selected))
                real = [s for s in selected if s['phase'] == 'real']
                arms = {a: [s['metrics'][name][clock] for s in real if s['arm'] == a] for a in ('base', 'treat')}
                paired = [st.fmean(s['metrics'][name][clock] for s in real if s['round'] == r and s['arm'] == 'treat') -
                          st.fmean(s['metrics'][name][clock] for s in real if s['round'] == r and s['arm'] == 'base')
                          for r in sorted({s['round'] for s in real})]
                bootstrap = sorted(st.fmean(rng.choices(paired, k=len(paired))) for _ in range(4000))
                delta = st.fmean(paired)
                ci = [bootstrap[100], bootstrap[3899]] if len(paired) >= MIN_ROUNDS else None
                analysis = analyse(raw, 1.0, split_modes=False)
                controls = {phase: ([-100 * v / (100 + v) for v in reversed(analysis['phases'][phase]['ci95'])]
                                   if phase in analysis['phases'] else None)
                            for phase in ('null', 'path')}
                clear = all(value is not None and -1 < value[0] and value[1] < 1 for value in controls.values())
                if not clear and len(paired) >= MIN_ROUNDS:
                    analysis['verdict'] = 'unresolved'
                output[f'{mode}/{name}/{clock}'] = dict(primary=analysis, controls_clear=clear,
                    mode_sensitivity=analyse(raw, 1.0), delta_us=delta, ci95_us=ci, samples_us=arms)
                def formatted(xs):
                    return f'{st.fmean(xs):.3f} ± {st.stdev(xs):.3f}'
                interval = f'[{ci[0]:+.3f}, {ci[1]:+.3f}]' if ci else 'N/A (too few rounds)'
                text.append(f'| {mode} | {name} | {clock} | {formatted(arms["base"])} | '
                            f'{formatted(arms["treat"])} | {delta:+.3f} {interval} | '
                            + ' | '.join(f'[{v[0]:+.2f}, {v[1]:+.2f}]' if v else 'N/A' for v in controls.values())
                            + f' | {"clear" if clear else "unresolved"} |')
    (run / 'analysis.json').write_text(json.dumps(output, indent=2))
    (run / 'summary.md').write_text('\n'.join(text) + '\n')


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--base', type=Path)
    p.add_argument('--head', type=Path)
    p.add_argument('--rounds', type=int, default=16)
    p.add_argument('--control-rounds', type=int, default=8)
    p.add_argument('--messages', type=int, default=400)
    p.add_argument('--connections', type=int, default=20)
    p.add_argument('--protocols', default='h1,h1tls,h2tls')
    p.add_argument('--cases', default='http,http-large,http-churn,http-large-churn,text,binary,connect,close,lifecycle')
    p.add_argument('--gc', choices=('on', 'off', 'both'), default='both')
    p.add_argument('--client-cpu', type=int, default=4)
    p.add_argument('--peer-cpu', type=int, default=6)
    p.add_argument('--probe', type=Path)
    p.add_argument('--peer', type=Path)
    p.add_argument('--ports', type=json.loads)
    p.add_argument('--cert')
    p.add_argument('--key')
    p.add_argument('--instructions', action='store_true')
    args = p.parse_args()
    if sys.version_info < (3, 12):
        p.error('needs Python 3.12+ for sys.monitoring')
    if args.peer or args.probe:
        os.sched_setaffinity(0, {args.peer_cpu if args.peer else args.client_cpu})
        if args.peer:
            asyncio.run(peer(args))
        else:
            print(json.dumps(asyncio.run(probe(args))))
        return
    if not args.base or not args.head or min(args.rounds, args.control_rounds, args.messages, args.connections) < 1:
        p.error('base/head and positive rounds/messages/connections required')
    if args.client_cpu == args.peer_cpu:
        p.error('client and peer CPUs must differ')
    topology = {}
    for cpu in (args.client_cpu, args.peer_cpu):
        directory = Path(f'/sys/devices/system/cpu/cpu{cpu}/topology')
        topology[cpu] = {name: (directory / name).read_text().strip()
                         for name in ('physical_package_id', 'core_id', 'thread_siblings_list')}
    if all(topology[args.client_cpu][name] == topology[args.peer_cpu][name]
           for name in ('physical_package_id', 'core_id')):
        p.error('client and peer must use different physical cores')
    sys.path.insert(0, str(ROOT))
    from bench.lib.results import run_dir
    from testing_client import bytecode
    run = run_dir('websocket_client', {k: str(v) for k, v in vars(args).items()})
    for relative in ('bench/micro/websocket_client.py', 'bench/micro/testing_client.py',
                     'bench/ab/hpack_encode.py', 'bench/ab/report.py', 'bench/lib/results.py'):
        target = run / 'harness' / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / relative, target)
    (run / 'environment.json').write_text(json.dumps(dict(python=sys.version,
        platform=platform.platform(), openssl=ssl.OPENSSL_VERSION), indent=2))
    (run / 'configuration.json').write_text(json.dumps(
        {k: str(v) for k, v in vars(args).items()}, indent=2))
    (run / 'packages.json').write_text(json.dumps(
        {d.metadata['Name']: d.version for d in importlib.metadata.distributions()}, indent=2))
    (run / 'topology.json').write_text(json.dumps(topology, indent=2))
    roots = {arm: run / 'tmp' / arm for arm in ('base', 'treat')}
    copy_tree(args.base, roots['base'])
    copy_tree(args.base, roots['treat'])
    saved = run / 'tmp' / 'saved-treatment'
    copy_tree(args.head, saved)
    states = {arm: bytecode(root, FILES) for arm, root in (('base', roots['base']), ('treat', saved))}
    (run / 'bytecode.json').write_text(json.dumps(states, indent=2))
    cert, key = run / 'tmp' / 'cert.pem', run / 'tmp' / 'key.pem'
    subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-days', '1',
                    '-subj', '/CN=localhost', '-keyout', str(key), '-out', str(cert)],
                   check=True, capture_output=True, timeout=30)
    env = {**os.environ, 'PYTHONHASHSEED': '0', 'BB_H2_ENABLE_WEBSOCKET': '1',
           'BB_UVLOOP': '0', 'BB_ACCESS_LOG': '0', 'BB_WORKERS': '1', 'PYTHONDONTWRITEBYTECODE': '1'}
    common = ['--client-cpu', str(args.client_cpu), '--peer-cpu', str(args.peer_cpu)]
    with (run / 'peer.log').open('w') as errors:
        server = subprocess.Popen([sys.executable, __file__, '--peer', str(roots['base']),
            '--cert', str(cert), '--key', str(key), *common], stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=errors, text=True, env=env)
        try:
            if not select.select([server.stdout], [], [], 15)[0]:
                raise TimeoutError('peer readiness; see peer.log')
            ready = json.loads(server.stdout.readline())
            (run / 'peer.json').write_text(json.dumps(ready, indent=2))
            def invoke(root, mode, protocol, case, instructions):
                command = [sys.executable, __file__, '--probe', str(root), '--ports', json.dumps(ready),
                    '--messages', str(min(args.messages, 20) if instructions else args.messages),
                    '--connections', str(min(args.connections, 3) if instructions else args.connections),
                    '--protocols', protocol, '--cases', case, '--gc', mode, *common]
                if instructions:
                    command.append('--instructions')
                return json.loads(subprocess.check_output(command, env=env, text=True, timeout=90))
            def child(root, mode, instructions=False):
                merged = None
                for protocol in args.protocols.split(','):
                    for case in args.cases.split(','):
                        sample = invoke(root, mode, protocol, case, instructions)
                        if merged is None:
                            merged = {**sample, 'metrics': {}, 'source_proof': {}}
                        assert sample['compiled_methods'] == merged['compiled_methods']
                        for name, digest in sample['source_proof'].items():
                            assert merged['source_proof'].get(name, digest) == digest
                            merged['source_proof'][name] = digest
                        for name, metric in sample['metrics'].items():
                            merged['metrics'][name] = {**metric, 'process_id': sample['process_id']}
                return merged
            rows = []
            with (run / 'samples.jsonl').open('w') as output:
                for phase in ('null', 'path', 'real'):
                    if phase == 'real':
                        copy_tree(saved, roots['treat'])
                    for mode in (('on', 'off') if args.gc == 'both' else (args.gc,)):
                        for rnd in range(args.rounds if phase == 'real' else args.control_rounds):
                            for arm in (('base', 'treat', 'treat', 'base') if rnd % 2 == 0 else ('treat', 'base', 'base', 'treat')):
                                row = dict(phase=phase, round=rnd, arm=arm,
                                    **child(roots['base' if phase == 'null' else arm], mode))
                                output.write(json.dumps(row) + '\n')
                                output.flush()
                                rows.append(row)
                            print(f'{phase} gc={mode} round {rnd + 1} complete', flush=True)
                        print(f'{phase} gc={mode} complete', flush=True)
            (run / 'instructions.json').write_text(json.dumps(
                {arm: child(root, 'off', True) for arm, root in roots.items()}, indent=2))
            report(run, rows)
            (run / 'COMPLETE').write_text('exit_code=0\n')
            print(run)
        finally:
            server.stdin.close()
            try:
                server.wait(timeout=5)
            except subprocess.TimeoutExpired:
                server.kill()
                server.wait(timeout=5)


if __name__ == '__main__':
    main()
