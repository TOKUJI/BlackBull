"""TestClient ABBA screening and bytecode comparison across two worktrees.

python bench/micro/testing_client.py --base PATH --head PATH
Rates are operations/second, not wire-server throughput. Local results cannot
certify equivalence; inspect the A/A control and all samples before interpreting.
"""
from __future__ import annotations

import argparse
from collections import Counter
import dis
import hashlib
import json
import marshal
import os
from pathlib import Path
import subprocess
import sys
import time
import types

ROOT = Path(__file__).resolve().parents[2]


def bytecode(root):
    modules, functions = {}, {}
    for path in sorted(Path(root).glob('blackbull/**/*.py')):
        name = str(path.relative_to(root))
        code = compile(path.read_bytes(), name, 'exec')
        modules[name] = hashlib.sha256(marshal.dumps(code)).hexdigest()
        if name == 'blackbull/testing/__init__.py':
            def visit(code):
                functions[code.co_qualname] = [
                    [i.opname, i.argrepr] for i in dis.get_instructions(code)
                    if not isinstance(i.argval, types.CodeType)]
                for const in code.co_consts:
                    if isinstance(const, types.CodeType):
                        visit(const)
            visit(code)
    return {'modules': modules, 'functions': functions}


def probe(root, count, instructions):
    sys.meta_path = [f for f in sys.meta_path
                     if '__editable__' not in type(f).__module__
                     and '__editable__' not in getattr(f, '__name__', '')]
    sys.path.insert(0, root)
    import blackbull.testing as testing
    assert Path(testing.__file__).resolve() == Path(root) / 'blackbull/testing/__init__.py'

    async def app(conn, receive, send):
        kind = conn['type'] if isinstance(conn, dict) else conn.type
        if kind == 'lifespan':
            await receive()
            await send({'type': 'lifespan.startup.complete'})
            await receive()
            await send({'type': 'lifespan.shutdown.complete'})
        elif kind == 'http':
            await send({'type': 'http.response.start', 'status': 200, 'headers': []})
            await send({'type': 'http.response.body', 'body': b'ok'})
        else:
            await receive()
            await send({'type': 'websocket.accept'})
            while (event := await receive())['type'] != 'websocket.disconnect':
                await send({'type': 'websocket.send', 'text': event['text']})

    rates, counts = {}, {}
    current = Counter()
    monitor = getattr(sys, 'monitoring', None)
    if instructions:
        if monitor is None:
            raise RuntimeError('Instruction counting needs Python 3.12+')
        monitor.use_tool_id(monitor.PROFILER_ID, 'testing-client')
        def instruction(code, offset):
            if code.co_filename == testing.__file__:
                current[code.co_qualname] += 1
        monitor.register_callback(monitor.PROFILER_ID, monitor.events.INSTRUCTION, instruction)

    def measure(name, operation):
        for _ in range(min(count, 20)):
            operation()
        current.clear()
        if instructions:
            monitor.set_events(monitor.PROFILER_ID, monitor.events.INSTRUCTION)
        start = time.perf_counter_ns()
        for _ in range(count):
            operation()
        elapsed = time.perf_counter_ns() - start
        if instructions:
            monitor.set_events(monitor.PROFILER_ID, 0)
        rates[name] = count * 1e9 / elapsed
        counts[name] = {k: v / count for k, v in sorted(current.items())}

    def lifecycle():
        with testing.TestClient(app):
            pass

    measure('client_lifecycle', lifecycle)
    with testing.TestClient(app) as client:
        def http():
            response = client.get('/')
            assert response.status_code == 200 and response.content == b'ok'
        measure('http', http)
        def connect():
            with client.websocket_connect('/'):
                pass
        measure('ws_lifecycle', connect)
        with client.websocket_connect('/') as ws:
            def roundtrip():
                ws.send_text('echo')
                assert ws.receive_text() == 'echo'
            measure('ws_roundtrip', roundtrip)
    if instructions:
        monitor.free_tool_id(monitor.PROFILER_ID)
    return {'source': testing.__file__, 'sha256': hashlib.sha256(
        Path(testing.__file__).read_bytes()).hexdigest(), 'rates': rates, 'instructions': counts}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base')
    parser.add_argument('--head')
    parser.add_argument('--rounds', type=int, default=8)
    parser.add_argument('--count', type=int, default=300)
    parser.add_argument('--probe')
    parser.add_argument('--instructions', action='store_true')
    args = parser.parse_args()
    if args.probe:
        print(json.dumps(probe(str(Path(args.probe).resolve()), args.count, args.instructions)))
        return
    if not args.base or not args.head or args.rounds < 2 or args.count < 1:
        parser.error('base/head, rounds >= 2, count >= 1 required')
    sys.path.insert(0, str(ROOT))
    from bench.lib.results import run_dir
    from bench.ab.report import analyse, markdown
    roots = {'base': str(Path(args.base).resolve()), 'treat': str(Path(args.head).resolve())}
    run = run_dir('testing_client', {**vars(args), 'python': sys.version})
    states = {arm: bytecode(root) for arm, root in roots.items()}
    changed = sorted(k for k in states['base']['modules'].keys() | states['treat']['modules'].keys()
                     if states['base']['modules'].get(k) != states['treat']['modules'].get(k))
    (run / 'bytecode.json').write_text(json.dumps({'changed_modules': changed, **states}, indent=2))
    rows = []
    def child(root, instructions=False):
        command = [sys.executable, __file__, '--probe', root, '--count', str(args.count)]
        if instructions:
            command += ['--instructions']
        return json.loads(subprocess.check_output(command, text=True, timeout=120,
                          env={**os.environ, 'PYTHONHASHSEED': '0'}))
    with (run / 'samples.jsonl').open('w') as output:
        for phase in ('null', 'real'):
            for rnd in range(args.rounds):
                for arm in (('base', 'treat', 'treat', 'base') if rnd % 2 == 0
                            else ('treat', 'base', 'base', 'treat')):
                    sample = {'phase': phase, 'round': rnd, 'arm': arm,
                              **child(roots['base' if phase == 'null' else arm])}
                    rows.append(sample)
                    output.write(json.dumps(sample) + '\n')
                    output.flush()
    for name in rows[0]['rates']:
        raw = run / f'{name}.tsv'
        raw.write_text('phase\tround\tarm\tvalue\tproof\n' + ''.join(
            f"{s['phase']}\t{s['round']}\t{s['arm']}\t{s['rates'][name]}\t{s['sha256']}\n"
            for s in rows))
        (run / f'{name}.md').write_text(
            'Local screening only; all samples paired by round.\n\n' +
            markdown(analyse(raw, 1.0, split_modes=False)))
    if sys.version_info >= (3, 12):
        (run / 'instructions.json').write_text(json.dumps(
            {arm: child(root, True) for arm, root in roots.items()}, indent=2))
    print(run)


if __name__ == '__main__':
    main()
