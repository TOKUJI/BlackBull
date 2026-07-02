#!/usr/bin/env python3
"""Local WSL2 A/B harness for the unary-grpc cold-start fixes.

Two measurements, each launching the REAL bench launcher+app (scratch copy
without the /results logging ini) with a given env config on :8080 (h2c):

  ready mode  -- "regular load generator": a light prober opens a fresh h2c
                 connection every 20ms from t0 (launch) and issues GetSum.
                 Reports time-to-first-OK (the user-facing waiting time) and
                 how many attempts were refused/failed before that.

  burst mode  -- "burst connections": as soon as the TCP port is connectable
                 (socket bound), fire M concurrent GetSum calls with a fixed
                 deadline.  Reports ok / refused / deadline / other, i.e. how
                 many connections are lost.

Run:  python readiness_ab.py partA
      python readiness_ab.py partB
"""
from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from concurrent import futures

import grpc
import benchmark_pb2 as pb
import benchmark_pb2_grpc as pbg

HERE = os.path.dirname(os.path.abspath(__file__))
BENCH_DIR = os.path.dirname(HERE)  # bench/httparena/


def _provision_benchapp():
    """Copy the real bench launcher/app into a temp dir WITHOUT the container's
    logging_access.ini (its FileHandler targets /results, absent locally)."""
    d = tempfile.mkdtemp(prefix='ab_benchapp_')
    for f in ('launcher.py', 'app.py', 'db.py', 'grpc_bench.py'):
        shutil.copy(os.path.join(BENCH_DIR, f), d)
    return os.path.join(d, 'launcher.py')


LAUNCHER = _provision_benchapp()
_APP_DIR = os.path.dirname(LAUNCHER)
HOST, PORT = '127.0.0.1', 8080
WORKERS = int(os.environ.get('AB_WORKERS', '4'))

# grpc channel options: aggressive reconnect so a refused probe retries fast
# (models an eager load generator, keeps timing resolution fine).
_CH_OPTS = [('grpc.initial_reconnect_backoff_ms', 100),
            ('grpc.min_reconnect_backoff_ms', 100),
            ('grpc.max_reconnect_backoff_ms', 200)]


def _kill_stragglers():
    subprocess.run(['pkill', '-f', f'{_APP_DIR}/launcher.py'],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    subprocess.run(['pkill', '-f', f'{_APP_DIR}/app.py'],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def launch(env_overrides):
    _kill_stragglers()
    time.sleep(0.4)  # let the port free
    env = os.environ.copy()
    env['BB_HTTPARENA_PORTS'] = 'http'
    env['WEB_WORKERS'] = str(WORKERS)
    env.setdefault('BB_GRPC_WARMUP', '0')
    env.setdefault('BB_SOCKET_BACKLOG', '1024')
    env.pop('BB_EARLY_BIND', None)
    env.pop('BB_ACCEPT_THREAD', None)
    env.update(env_overrides)
    t0 = time.monotonic()
    proc = subprocess.Popen([sys.executable, LAUNCHER], env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return proc, t0


def teardown(proc):
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
    _kill_stragglers()


def _getsum(timeout, wait_for_ready=False):
    """One fresh-connection GetSum. Returns ('ok'|'refused'|'deadline'|'other', detail).

    wait_for_ready=True models a patient load generator: the call rides through
    connect retries (kernel backlog + grpc reconnect) until the server answers
    or the deadline fires -- so a queued-but-not-yet-served connection becomes a
    DEADLINE loss only if the server is still not serving by the deadline.
    """
    ch = grpc.insecure_channel(f'{HOST}:{PORT}', options=_CH_OPTS)
    try:
        r = ch.unary_unary(
            '/benchmark.BenchmarkService/GetSum',
            request_serializer=pb.SumRequest.SerializeToString,
            response_deserializer=pb.SumReply.FromString,
        )(pb.SumRequest(a=1, b=2), timeout=timeout, wait_for_ready=wait_for_ready)
        return 'ok', r.result
    except grpc.RpcError as e:
        code = e.code()
        if code == grpc.StatusCode.DEADLINE_EXCEEDED:
            return 'deadline', ''
        det = (e.details() or '').lower()
        if 'refused' in det or 'connect' in det or code == grpc.StatusCode.UNAVAILABLE:
            return 'refused', det[:40]
        return 'other', f'{code.name}:{det[:30]}'
    finally:
        ch.close()


# --------------------------------------------------------------------------
# ready mode — time-to-first-OK
# --------------------------------------------------------------------------

def measure_ready(t0, overall_timeout=25.0, cadence=0.02, call_timeout=0.4):
    fails = 0
    first_fail_kind = None
    deadline = t0 + overall_timeout
    while time.monotonic() < deadline:
        kind, _ = _getsum(call_timeout)
        if kind == 'ok':
            return time.monotonic() - t0, fails, first_fail_kind
        fails += 1
        if first_fail_kind is None:
            first_fail_kind = kind
        time.sleep(cadence)
    return None, fails, first_fail_kind


# --------------------------------------------------------------------------
# burst mode — fire M concurrent as soon as the port is bound
# --------------------------------------------------------------------------

def _wait_bound(t0, timeout=10.0):
    """Return seconds from t0 until the TCP port first accepts a connection."""
    deadline = t0 + timeout
    while time.monotonic() < deadline:
        try:
            c = socket.create_connection((HOST, PORT), timeout=0.3)
            c.close()
            return time.monotonic() - t0
        except OSError:
            time.sleep(0.01)
    return None


def measure_burst(t0, M=256, call_timeout=2.5):
    bound_at = _wait_bound(t0)
    if bound_at is None:
        return {'bound_at': None, 'ok': 0, 'refused': M, 'deadline': 0, 'other': 0,
                'first_ok': None, 'last_ok': None}
    fire = time.monotonic()
    counts = {'ok': 0, 'refused': 0, 'deadline': 0, 'other': 0}
    ok_times = []
    with futures.ThreadPoolExecutor(max_workers=M) as ex:
        futs = [ex.submit(_getsum, call_timeout, True) for _ in range(M)]
        for f in futures.as_completed(futs):
            kind, _ = f.result()
            counts[kind] += 1
            if kind == 'ok':
                ok_times.append(time.monotonic() - fire)
    return {'bound_at': bound_at, **counts,
            'first_ok': min(ok_times) if ok_times else None,
            'last_ok': max(ok_times) if ok_times else None}


# --------------------------------------------------------------------------
# configs
# --------------------------------------------------------------------------

PART_A = [
    ('WITHOUT_ANY_FIX (base)',        {}),
    ('EARLY_BIND + BACKLOG=4096',     {'BB_EARLY_BIND': '1', 'BB_SOCKET_BACKLOG': '4096'}),
]

PART_B = [
    ('EARLY_BIND + BACKLOG',              {'BB_EARLY_BIND': '1', 'BB_SOCKET_BACKLOG': '4096'}),
    ('EARLY_BIND + BACKLOG + ACCEPT_THREAD', {'BB_EARLY_BIND': '1', 'BB_SOCKET_BACKLOG': '4096', 'BB_ACCEPT_THREAD': '1'}),
    ('EARLY_BIND + BACKLOG + GRPC_WARMUP=3', {'BB_EARLY_BIND': '1', 'BB_SOCKET_BACKLOG': '4096', 'BB_GRPC_WARMUP': '3'}),
]

REPEATS = int(os.environ.get('AB_REPEATS', '3'))


def run_part_a():
    print(f'\n=== Part A — regular load: time-to-ready (workers={WORKERS}, '
          f'{REPEATS} runs each) ===')
    print(f'{"config":32} {"t_first_ok(s)":>14} {"fails_before":>13} {"first_fail":>11}')
    print('-' * 74)
    for label, env in PART_A:
        rows = []
        for _ in range(REPEATS):
            proc, t0 = launch(env)
            try:
                t_ok, fails, kind = measure_ready(t0)
            finally:
                teardown(proc)
            rows.append((t_ok, fails, kind))
            time.sleep(0.5)
        oks = [r[0] for r in rows if r[0] is not None]
        med = sorted(oks)[len(oks) // 2] if oks else None
        fails_med = sorted(r[1] for r in rows)[len(rows) // 2]
        kind = next((r[2] for r in rows if r[2]), '-')
        med_s = f'{med:.2f}' if med is not None else 'TIMEOUT'
        print(f'{label:32} {med_s:>14} {fails_med:>13} {kind:>11}')


def run_part_b():
    M = int(os.environ.get('AB_BURST', '256'))
    D = float(os.environ.get('AB_DEADLINE', '2.5'))
    print(f'\n=== Part B — burst of {M} at bind, wait_for_ready, deadline={D}s: '
          f'losses (workers={WORKERS}, {REPEATS} runs each) ===')
    print(f'{"config":38} {"bound(s)":>9} {"ok":>5} {"refused":>8} {"deadline":>9} '
          f'{"other":>6} {"drain_last(s)":>13}')
    print('-' * 96)
    for label, env in PART_B:
        agg = {'ok': [], 'refused': [], 'deadline': [], 'other': [],
               'bound_at': [], 'last_ok': []}
        for _ in range(REPEATS):
            proc, t0 = launch(env)
            try:
                r = measure_burst(t0, M=M, call_timeout=D)
            finally:
                teardown(proc)
            for k in ('ok', 'refused', 'deadline', 'other'):
                agg[k].append(r[k])
            if r['bound_at'] is not None:
                agg['bound_at'].append(r['bound_at'])
            if r['last_ok'] is not None:
                agg['last_ok'].append(r['last_ok'])
            time.sleep(0.5)

        def med(xs):
            return sorted(xs)[len(xs) // 2] if xs else None
        b = med(agg['bound_at'])
        dl = med(agg['last_ok'])
        b_s = f'{b:.2f}' if b is not None else '-'
        dl_s = f'{dl:.2f}' if dl is not None else '-'
        print(f'{label:38} {b_s:>9} {med(agg["ok"]):>5} {med(agg["refused"]):>8} '
              f'{med(agg["deadline"]):>9} {med(agg["other"]):>6} {dl_s:>13}')


if __name__ == '__main__':
    which = sys.argv[1] if len(sys.argv) > 1 else 'both'
    if which in ('partA', 'both'):
        run_part_a()
    if which in ('partB', 'both'):
        run_part_b()
    _kill_stragglers()
