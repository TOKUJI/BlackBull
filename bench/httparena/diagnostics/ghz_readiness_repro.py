#!/usr/bin/env python3
"""Reproduce the HttpArena gRPC readiness false-positive with the ACTUAL tool.

HttpArena's `_wait_grpc` treats `ghz ... BenchmarkService/GetSum -c1 -n1`
EXIT CODE 0 as "gRPC server ready".  This script drives that exact command
against five server states and prints ghz's exit code + the resulting verdict.
Because ghz is a load tester, it exits 0 even when the single RPC fails with
DeadlineExceeded or Unavailable (connection refused) -- so every state, even
"nothing bound", is reported ready.

Requires:  ghz (set GHZ=/path/to/ghz, default 'ghz' on PATH),
           python grpcio + grpcio-tools.
The proto stubs are generated in-process, so only benchmark.proto is needed.
"""
from __future__ import annotations

import importlib
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
from concurrent import futures

HOST = '127.0.0.1'
GHZ = os.environ.get('GHZ', 'ghz')
HERE = os.path.dirname(os.path.abspath(__file__))
PROTO = os.path.join(HERE, 'benchmark.proto')


def _load_stubs():
    """Generate benchmark_pb2 / _pb2_grpc from the proto at runtime."""
    out = tempfile.mkdtemp(prefix='ghz_repro_')
    from grpc_tools import protoc
    rc = protoc.main([
        'protoc', f'-I{HERE}', f'--python_out={out}',
        f'--grpc_python_out={out}', PROTO,
    ])
    if rc != 0:
        sys.exit('protoc failed to generate stubs')
    sys.path.insert(0, out)
    return importlib.import_module('benchmark_pb2'), importlib.import_module('benchmark_pb2_grpc')


import grpc  # noqa: E402
pb2, pb2_grpc = _load_stubs()


# ---- server variants -----------------------------------------------------

class _RealService(pb2_grpc.BenchmarkServiceServicer):
    def GetSum(self, request, context):
        return pb2.SumReply(result=request.a + request.b)


def start_real():
    srv = grpc.server(futures.ThreadPoolExecutor(max_workers=4))
    pb2_grpc.add_BenchmarkServiceServicer_to_server(_RealService(), srv)
    port = srv.add_insecure_port(f'{HOST}:0')
    srv.start()
    return port, lambda: srv.stop(0)


def _raw_listen():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind((HOST, 0))
    s.listen(128)
    return s


def start_never_accept():
    s = _raw_listen()
    port = s.getsockname()[1]
    stop = threading.Event()
    threading.Thread(target=lambda: [time.sleep(0.05)
                     for _ in iter(lambda: not stop.is_set(), False)],
                     daemon=True).start()
    return port, lambda: (stop.set(), s.close())


def start_accept_silent():
    s = _raw_listen()
    port = s.getsockname()[1]
    stop = threading.Event()
    held = []

    def loop():
        s.settimeout(0.3)
        while not stop.is_set():
            try:
                held.append(s.accept()[0])
            except socket.timeout:
                continue
            except OSError:
                break
    threading.Thread(target=loop, daemon=True).start()
    return port, lambda: (stop.set(), s.close())


def start_settings_only():
    """accept(), send a valid empty HTTP/2 SETTINGS frame, never answer."""
    s = _raw_listen()
    port = s.getsockname()[1]
    stop = threading.Event()
    held = []
    SETTINGS = bytes([0, 0, 0, 0x4, 0, 0, 0, 0, 0])  # len=0 type=SETTINGS

    def loop():
        s.settimeout(0.3)
        while not stop.is_set():
            try:
                c, _ = s.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            held.append(c)
            try:
                c.sendall(SETTINGS)
            except OSError:
                pass
    threading.Thread(target=loop, daemon=True).start()
    return port, lambda: (stop.set(), s.close())


def start_no_listener():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind((HOST, 0))
    port = s.getsockname()[1]
    s.close()
    return port, lambda: None


# ---- the exact _wait_grpc ghz probe --------------------------------------

def ghz_probe(port):
    cmd = [GHZ, '--insecure', '--proto', PROTO,
           '--call', 'benchmark.BenchmarkService/GetSum',
           '-d', '{"a":1,"b":2}', '-c', '1', '-n', '1', f'{HOST}:{port}']
    t0 = time.monotonic()
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=40)
        return p.returncode, p.stdout + p.stderr, time.monotonic() - t0
    except subprocess.TimeoutExpired:
        return None, '(subprocess timeout >40s)', time.monotonic() - t0


def _status(text):
    for line in text.splitlines():
        s = line.strip()
        if s.startswith('[') and 'responses' in s:
            return s
    return '(no status line)'


VARIANTS = [
    ('real          (serves GetSum)', start_real),
    ('never_accept  (bind+listen, no accept)', start_never_accept),
    ('accept_silent (accept, no bytes)', start_accept_silent),
    ('settings_only (accept + h2 SETTINGS)', start_settings_only),
    ('no_listener   (nothing bound)', start_no_listener),
]


def main():
    _v = subprocess.run([GHZ, '--version'], capture_output=True, text=True)
    ver = (_v.stdout + _v.stderr).strip()
    print(f'ghz {ver}  |  readiness == (ghz exit code 0)\n')
    print(f'{"variant":40} {"ghz_exit":>8} {"_wait_grpc":>12} {"secs":>6}   RPC status')
    print('-' * 96)
    for label, starter in VARIANTS:
        port, stop = starter()
        time.sleep(0.3)
        rc, out, dt = ghz_probe(port)
        verdict = 'ready ✅' if rc == 0 else 'retry ❌'
        rc_s = 'TIMEOUT' if rc is None else str(rc)
        print(f'{label:40} {rc_s:>8} {verdict:>12} {dt:6.2f}   {_status(out)}')
        try:
            stop()
        except Exception:
            pass
        time.sleep(0.15)
    print('-' * 96)
    print('\nAll five states -> ghz exit 0 -> "gRPC server ready". The exit code '
          'does not\nreflect the RPC status, so _wait_grpc never actually waits.')


if __name__ == '__main__':
    main()
