#!/usr/bin/env python3
"""Executed bytecode instructions per request — the parameter-free instrument.

The bytecode *size* diff (``disasm_hot.py``) counts instructions present in a
code object, not instructions run, so turning it into a time needs two guesses:
how much of each function executes, and how long an instruction takes.  Two
free parameters fitted to one measured number is not evidence.

``sys.monitoring`` removes both.  Count the instructions the server actually
executes while serving N requests, on two checkouts, and the comparison is a
ratio:

    Δ executed instructions / executed instructions

which is directly comparable to the A/B's Δ throughput, with no ns/instruction
constant anywhere.  If the added checks cost what the residual says they cost,
these two percentages agree; if they do not, the residual is something else.

Counts are attributed per file so "ours" (blackbull) can be separated from the
generator-facing asyncio machinery.

    BB_REPO=/tmp/bb-instr-base python count_instructions.py --lane h2 --n 400
"""
from __future__ import annotations

import argparse
import asyncio
import collections
import json
import os
import tempfile
import sys

REPO = os.environ.get('BB_REPO', '/home/toshio/work/BlackBull')
MAIN = '/home/toshio/work/BlackBull'
# The editable install's finder outranks sys.path; drop it, then prove below
# which tree was imported.
sys.meta_path = [f for f in sys.meta_path
                 if '__editable__' not in type(f).__module__
                 and '__editable__' not in getattr(f, '__name__', '')]
sys.path.insert(0, REPO)

os.environ.setdefault('BB_UVLOOP', '0')
os.environ.setdefault('BB_ACCESS_LOG', '0')
os.environ.setdefault('BB_WORKERS', '1')
os.environ.setdefault('BB_H2_INITIAL_WINDOW_SIZE', '65535')
os.environ.setdefault('BB_H2_CONNECTION_WINDOW_SIZE', '65535')

import blackbull                                    # noqa: E402
from blackbull.server import ASGIServer             # noqa: E402

sys.path.insert(0, MAIN)
#: ``BB_APP=<path/to/app.py>`` measures that file's ``app`` instead.
if os.environ.get('BB_APP'):
    import importlib                                # noqa: E402
    _app_path = os.path.abspath(os.environ['BB_APP'])
    sys.path.insert(0, os.path.dirname(_app_path))
    app = importlib.import_module(
        os.path.basename(_app_path).removesuffix('.py')).app
else:
    from bench.peers.native_app import app          # noqa: E402

MON = sys.monitoring
TOOL = MON.PROFILER_ID
_counts: collections.Counter = collections.Counter()
_on = False


_BY_FUNC = os.environ.get('BB_BY_FUNC', '')
_HTTPARENA = os.path.expanduser(os.environ.get('BB_HTTPARENA', '~/HttpArena'))
#: wrk lanes: path and Lua script.  ``churn`` is the EC2 A/B's
#: one-request-per-connection lane.
_WRK_LANES = {
    'conn': ('/conn', None),
    'plaintext': ('/plaintext', None),
    'churn': ('/plaintext', f'{MAIN}/bench/wrk/no_keepalive.lua'),
    'arena-baseline': ('/', f'{MAIN}/bench/wrk/httparena_baseline.lua'),
    'arena-json-comp': ('/', f'{MAIN}/bench/wrk/httparena_json_comp.lua'),
    'arena-static': ('/', f'{_HTTPARENA}/requests/static-rotate.lua'),
}
#: h2load lanes: request paths, or ``None`` for HttpArena's static-h2 URI list.
_H2_LANES = {
    'h2': '/1kb',
    'arena-h2': '/baseline2?a=1&b=1',
    'arena-static-h2': None,
}


def _instr(code, offset):
    if _on:
        if _BY_FUNC and _BY_FUNC in code.co_filename:
            _counts[f'{code.co_filename}::{code.co_qualname}'] += 1
        else:
            _counts[code.co_filename] += 1
    return MON.DISABLE if not _on else None


#: With ``BB_C_CALLS=1``, calls into C functions are counted instead, by name:
#: the syscalls and builtins that instruction counts cannot see.
_C_CALLS = os.environ.get('BB_C_CALLS') == '1'


def _call(code, offset, callable_, arg0):
    if _on and type(callable_).__name__ in ('builtin_function_or_method',
                                            'method_descriptor'):
        _counts[f'C::{getattr(callable_, "__qualname__", callable_)}'] += 1


#: With ``BB_CALLERS=a,b``, each entry into code whose file contains one of
#: the substrings is counted by its nearest caller outside those files.
_CALLERS = tuple(t for t in os.environ.get('BB_CALLERS', '').split(',') if t)


def _start(code, offset):
    if not _on or not any(t in code.co_filename for t in _CALLERS):
        return None
    frame = sys._getframe(1)
    while frame and any(t in frame.f_code.co_filename for t in _CALLERS):
        frame = frame.f_back
    where = (f'{frame.f_code.co_filename.rsplit("/", 1)[-1]}:{frame.f_lineno}'
             f' {frame.f_code.co_qualname}' if frame else '?')
    _counts[f'{code.co_qualname} <- {where}'] += 1
    return None


#: With ``BB_GC=1``, nothing is traced: garbage collections and the time
#: spent in them are counted instead, per generation.
_GC = os.environ.get('BB_GC') == '1'
_gc_started: dict = {}


def _gc_event(phase, info):
    import time
    if not _on:
        return
    if phase == 'start':
        _gc_started['t'] = time.perf_counter()
    else:
        gen = info.get('generation')
        _counts[f'gc::collections_gen{gen}'] += 1
        _counts[f'gc::usec_gen{gen}'] += int(
            (time.perf_counter() - _gc_started.get('t', time.perf_counter())) * 1e6)


def _arm() -> None:
    if _GC:
        import gc
        gc.callbacks.append(_gc_event)
        return
    MON.use_tool_id(TOOL, 'instr-count')
    if _CALLERS:
        MON.register_callback(TOOL, MON.events.PY_START, _start)
        MON.set_events(TOOL, MON.events.PY_START)
        return
    if _C_CALLS:
        MON.register_callback(TOOL, MON.events.CALL, _call)
        MON.set_events(TOOL, MON.events.CALL)
        return
    MON.register_callback(TOOL, MON.events.INSTRUCTION, _instr)
    MON.set_events(TOOL, MON.events.INSTRUCTION)


def _disarm() -> None:
    if _GC:
        import gc
        gc.callbacks.remove(_gc_event)
        return
    MON.set_events(TOOL, 0)
    MON.free_tool_id(TOOL)


async def _gen(lane: str, port: int, n: int) -> int:
    """Drive *n* requests from a subprocess and report how many completed.

    Out of process on purpose: monitoring counts every instruction executed in
    *this* interpreter, so an in-process client would land in the denominator
    and shrink every percentage by its own share.
    """
    # Concurrency is env-overridable because it is not a free parameter: the
    # EC2 A/B runs wrk -t4 -c32 and h2load -c32 -m16, and the per-loop-iteration
    # overhead (selectors, _run_once) amortises differently at c=1 than at
    # c=32 — which moves the *denominator* of every ratio computed here.
    if lane in _WRK_LANES:
        path, script = _WRK_LANES[lane]
        cmd = ['wrk', f'-t{os.environ.get("BB_WRK_THREADS", "1")}',
               f'-c{os.environ.get("BB_WRK_CONNS", "1")}', f'-d{n}s',
               *(['-s', script] if script else []),
               f'http://127.0.0.1:{port}{path}']
    else:
        path = _H2_LANES[lane]
        if path is None:
            with open(f'{_HTTPARENA}/requests/static-h2-uris.txt') as fh:
                uris = fh.read().replace('https://localhost:8443',
                                         f'http://127.0.0.1:{port}')
            uri_file = tempfile.NamedTemporaryFile('w', suffix='.txt', delete=False)
            uri_file.write(uris)
            uri_file.close()
            target = ['-i', uri_file.name]
        else:
            target = [f'http://127.0.0.1:{port}{path}']
        cmd = ['h2load', '-c', os.environ.get('BB_H2_CONNS', '1'),
               '-m', os.environ.get('BB_H2_STREAMS', '1'), '-n', str(n), *target]
    p = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT)
    out, _ = await p.communicate()
    if lane in _H2_LANES and _H2_LANES[lane] is None:
        os.unlink(target[1])
    text = out.decode()
    if lane in _WRK_LANES:
        for line in text.splitlines():
            if 'requests in' in line:
                return int(line.split()[0])
        raise SystemExit(f'wrk gave no request count:\n{text}')
    for line in text.splitlines():
        if line.startswith('requests:'):
            return int(line.split()[1])
    raise SystemExit(f'h2load gave no request count:\n{text}')


async def _run(lane: str, n: int) -> int:
    global _on
    server = ASGIServer(app)
    server.open_socket(0)
    port = server.port
    task = asyncio.create_task(server.run())
    await asyncio.sleep(0.4)
    await _gen(lane, port, 3 if lane in _WRK_LANES else 50)   # warm-up, unmonitored
    _arm()
    _on = True
    served = await _gen(lane, port, n)
    _on = False
    _disarm()
    task.cancel()
    server.close()
    return served


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument('--lane', choices=(*_WRK_LANES, *_H2_LANES), default='conn')
    ap.add_argument('--n', type=int, default=300)
    ap.add_argument('--out', required=True)
    args = ap.parse_args()

    print(f'imported blackbull from {blackbull.__file__}')
    served = asyncio.run(_run(args.lane, args.n))

    bb = sum(v for k, v in _counts.items() if '/blackbull/' in k)
    total = sum(_counts.values())
    rows = sorted(_counts.items(), key=lambda kv: -kv[1])[:12]
    print(f'RESULT lane={args.lane} served={served}')
    print(f'RESULT   blackbull instrs/req : {bb / served:10.1f}')
    print(f'RESULT   total     instrs/req : {total / served:10.1f}')
    for path, c in rows:
        print(f'RESULT     {c / served:9.1f}  {path.rsplit("/", 2)[-1]}')
    with open(args.out, 'w') as fh:
        json.dump({'lane': args.lane, 'served': served, 'blackbull': bb,
                   'total': total, 'per_file': dict(_counts)}, fh)


if __name__ == '__main__':
    main()
