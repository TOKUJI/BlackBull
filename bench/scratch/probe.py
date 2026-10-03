"""BLA-445 measurement instrument: the reload accept window vs the cold window.

Instrument validity first: the same burst in steady state must report 0
affected connections before any window number is believed, and a windowed run
must report more than 0 (the control run is the zero, the cold window is the
known positive).

The server is spawned through a runner *file*, not ``python -c``: the reload
re-exec runs ``[sys.executable, *sys.argv]``, which for a ``-c`` spawn is
``python -c app:app ...`` — the string ``app:app`` is a valid annotation
statement and a no-op, so the re-exec exits 0 and the probe would record a
vanished server as a window.  A runner file survives the re-exec unchanged.
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

APP = '''
import asyncio, os

from blackbull import BlackBull

app = BlackBull()


@app.route(path='/ready')
async def ready():
    return b'ready'


if os.environ.get('PARK_STARTUP'):
    @app.on_startup
    async def _park():
        await asyncio.sleep(float(os.environ['PARK_STARTUP']))
'''

RUNNER = 'from blackbull.cli import main; raise SystemExit(main())\n'
BURST = 40
SLOW = 0.5
TIMEOUT = 30.0
SOURCE = os.environ.get('PYTHONPATH', os.getcwd()).split(os.pathsep)[0]


class Conn:
    def __init__(self, family, addr, timeout=TIMEOUT):
        self.family, self.addr, self.timeout = family, addr, timeout

    def probe(self, t0=0.0):
        rec = {'t': time.monotonic() - t0}
        try:
            if self.family == 'unix':
                s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            else:
                s = socket.socket(
                    socket.AF_INET6 if ':' in self.addr[0] else socket.AF_INET,
                    socket.SOCK_STREAM)
            try:
                s.settimeout(self.timeout)
                s.connect(self.addr)
                rec['connected'] = True
                s.sendall(b'GET /ready HTTP/1.1\r\nHost: x\r\n\r\n')
                data = b''
                while b'\r\n\r\n' not in data:
                    chunk = s.recv(65536)
                    if not chunk:
                        rec['fate'] = 'eof'
                        return rec
                    data += chunk
                latency = time.monotonic() - t0 - rec['t']
                rec['latency'] = latency
                rec['fate'] = 'served-fast' if latency < SLOW else 'served-slow'
            finally:
                s.close()
        except ConnectionRefusedError:
            rec['fate'] = 'refused'
        except ConnectionResetError:
            rec['fate'] = 'reset'
        except OSError as e:
            rec['fate'] = f'oserror:{e.__class__.__name__}:{e.errno}'
        return rec

    def burst(self, n=BURST):
        results = []
        lock = threading.Lock()

        def one():
            r = self.probe()
            with lock:
                results.append(r)

        threads = [threading.Thread(target=one) for _ in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        return results


def listen_state(port_or_path, family):
    if family == 'unix':
        return 'exists' if os.path.exists(port_or_path) else 'gone'
    out = subprocess.run(['ss', '-ltn', 'sport', f'= :{port_or_path}'],
                         capture_output=True, text=True).stdout
    return 'LISTEN' if 'LISTEN' in out else 'gone'


def spawn(tmp: Path, bind: str, park: float, reload: bool):
    (tmp / 'app.py').write_text(APP)
    (tmp / 'run_server.py').write_text(RUNNER)
    env = dict(os.environ, WATCHFILES_FORCE_POLLING='1', PYTHONUNBUFFERED='1',
               PYTHONPATH=SOURCE, BB_ACCESS_LOG='0')
    if park:
        env['PARK_STARTUP'] = str(park)
    argv = [sys.executable, 'run_server.py', 'app:app', '--bind', bind]
    if reload:
        argv.append('--reload')
    log_fh = open(tmp / 'master.log', 'w', buffering=1)
    proc = subprocess.Popen(argv, env=env, cwd=str(tmp), stdout=log_fh,
                            stderr=subprocess.STDOUT, text=True,
                            start_new_session=True)
    log_fh.close()
    return proc


def wait_serving(conn, proc, deadline_s):
    deadline = time.monotonic() + deadline_s
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(f'server died (exit {proc.returncode})')
        if conn.probe()['fate'].startswith('served'):
            return time.monotonic()
        time.sleep(0.2)
    raise RuntimeError('server never served')


def fates(records):
    out = {}
    for r in records:
        out[r['fate']] = out.get(r['fate'], 0) + 1
    return out


def run(family, bind, addr, port_or_path, backlog, park, reload, warm=5,
        client_timeout=TIMEOUT, active_warm=False):
    tmp = Path(f'/tmp/bla445/v2-{family}-{backlog}-{park}')
    os.makedirs(tmp, exist_ok=True)
    if family == 'unix':
        Path(port_or_path).unlink(missing_ok=True)
    proc = spawn(tmp, bind, park, reload)
    conn = Conn(family, addr, client_timeout)
    out = {'family': family, 'backlog': backlog, 'park': park,
           'reload': reload, 'burst': BURST, 'client_timeout': client_timeout,
           'warm': warm, 'active_warm': active_warm}

    # Cold window: the burst lands mid-park (or immediately without one).
    t_spawn = time.monotonic()
    time.sleep(min(park / 3.0, 1.0) if park else 0.02)
    cold = conn.burst()
    first = min((r['t'] + r.get('latency', 0) for r in cold
                 if r['fate'].startswith('served')), default=None)
    out['cold_window'] = {
        'window_s': (first - t_spawn) if first else None,
        'fates': fates(cold),
        'listen': listen_state(port_or_path, family),
    }

    wait_serving(conn, proc, park + 30)
    time.sleep(0.3)

    # Control: the same burst in steady state.  0 affected is the instrument's
    # zero; anything else invalidates the window numbers.
    control = conn.burst()
    out['control'] = {
        'affected': sum(1 for r in control
                        if not r['fate'].startswith('served-fast')),
        'fates': fates(control),
    }

    if not reload:
        proc.terminate()
        proc.wait(timeout=30)
        return out

    # Reload window on a socket that already has clients.
    held = []
    for _ in range(warm):
        try:
            if family == 'unix':
                s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            else:
                s = socket.socket(
                    socket.AF_INET6 if ':' in addr[0] else socket.AF_INET,
                    socket.SOCK_STREAM)
            s.settimeout(5.0)
            s.connect(addr)
            s.sendall(b'GET /ready HTTP/1.1\r\nHost: x\r\n\r\n')
            while b'\r\n\r\n' not in s.recv(65536):
                pass
            held.append(s)
        except OSError:
            pass

    eof_times = []
    lock = threading.Lock()

    def watch(s):
        s.settimeout(TIMEOUT)
        try:
            if active_warm:
                # A client that keeps sending: the keep-alive idle timeout
                # never fires, so the connection ends only at the drain
                # deadline -- the window's upper bound.
                while True:
                    s.sendall(b'GET /ready HTTP/1.1\r\nHost: x\r\n\r\n')
                    data = b''
                    while b'\r\n\r\n' not in data:
                        chunk = s.recv(65536)
                        if not chunk:
                            raise OSError('eof')
                        data += chunk
                    length = 0
                    for line in data.split(b'\r\n')[1:]:
                        if line.lower().startswith(b'content-length:'):
                            length = int(line.split(b':', 1)[1])
                    body = data.split(b'\r\n\r\n', 1)[1]
                    while len(body) < length:
                        body += s.recv(65536)
                    time.sleep(1.0)
            else:
                while s.recv(65536):
                    pass
        except OSError:
            pass
        with lock:
            eof_times.append(time.monotonic())

    eof_threads = [threading.Thread(target=watch, args=(s,)) for s in held]
    for t in eof_threads:
        t.start()

    time.sleep(0.3)
    t_trigger = time.monotonic()
    (tmp / 'app.py').write_text(APP + f'# reload trigger {t_trigger}\n')
    time.sleep(min(park / 3.0, 1.0) if park else 1.0)
    reloaded = conn.burst()
    first_new = min((r['t'] + r.get('latency', 0) for r in reloaded
                     if r['fate'].startswith('served')), default=None)
    for t in eof_threads:
        t.join(timeout=5)
    out['reload_window'] = {
        'window_s': (first_new - t_trigger) if first_new else None,
        'fates': fates(reloaded),
        'listen_during': listen_state(port_or_path, family),
        'warm_held': len(held),
        'warm_eof_after_trigger_s': sorted(t - t_trigger for t in eof_times),
    }
    proc.terminate()
    try:
        proc.wait(timeout=30)
    except subprocess.TimeoutExpired:
        proc.kill()
    return out


if __name__ == '__main__':
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument('--family', choices=['inet', 'inet6', 'unix'], default='inet')
    ap.add_argument('--backlog', type=int, default=1024)
    ap.add_argument('--park', type=float, default=6.0)
    ap.add_argument('--reload', action='store_true')
    ap.add_argument('--warm', type=int, default=5)
    ap.add_argument('--client-timeout', type=float, default=TIMEOUT)
    ap.add_argument('--active-warm', action='store_true')
    args = ap.parse_args()
    if args.family == 'unix':
        tmp = f'/tmp/bla445/v2-unix-{args.backlog}-{args.park}'
        os.makedirs(tmp, exist_ok=True)
        path = f'{tmp}/s.sock'
        bind, addr, where = f'unix:{path}', path, path
    else:
        with socket.socket(socket.AF_INET6 if args.family == 'inet6'
                           else socket.AF_INET) as s:
            s.bind(('::1' if args.family == 'inet6' else '127.0.0.1', 0))
            port = s.getsockname()[1]
        host = '::1' if args.family == 'inet6' else '127.0.0.1'
        bind = f'{host}:{port}'
        addr, where = (host, port), port
    print(json.dumps(run(args.family, bind, addr, where,
                         args.backlog, args.park, args.reload,
                         warm=args.warm, client_timeout=args.client_timeout,
                         active_warm=args.active_warm), indent=1))
