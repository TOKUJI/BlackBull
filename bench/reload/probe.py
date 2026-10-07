"""Measure startup and reload responses using fresh and held connections."""
from __future__ import annotations

import argparse
import http.client
import json
import math
import os
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path


def exchange(sock, timeout=30):
    expired = threading.Event()

    def expire():
        expired.set()
        shutdown(sock)

    timer = threading.Timer(timeout, expire)
    timer.daemon = True
    timer.start()
    try:
        sock.sendall(b'GET /ready HTTP/1.1\r\nHost: localhost\r\n\r\n')
        with http.client.HTTPResponse(sock) as response:
            response.begin()
            if response.status != 200 or response.getheader('Content-Length') != '2':
                raise ValueError('unexpected status or body length')
            body = response.read()
            if body not in (b'v1', b'v2'):
                raise ValueError('unexpected generation')
            if expired.is_set():
                raise TimeoutError('HTTP exchange deadline expired')
            return body.decode('ascii')
    except (OSError, http.client.HTTPException, ValueError):
        if expired.is_set():
            raise TimeoutError('HTTP exchange deadline expired') from None
        raise
    finally:
        timer.cancel()


def shutdown(sock):
    try:
        sock.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass


class Client:
    def __init__(self, family, address, timeout, slow):
        self.family, self.address = family, address
        self.timeout, self.slow = timeout, slow

    def connect(self):
        sock = socket.socket(self.family, socket.SOCK_STREAM)
        try:
            sock.settimeout(self.timeout)
            sock.connect(self.address)
            return sock
        except BaseException:
            sock.close()
            raise

    def probe(self, origin, scheduled=0):
        started = time.monotonic()
        record = {'scheduled': scheduled, 't': started - origin}
        try:
            with self.connect() as sock:
                record['generation'] = exchange(sock, self.timeout)
                record['latency'] = time.monotonic() - started
                record['fate'] = ('served-fast' if record['latency'] < self.slow
                                  else 'served-slow')
        except ConnectionRefusedError:
            record['fate'] = 'refused'
        except ConnectionResetError:
            record['fate'] = 'reset'
        except TimeoutError:
            record['fate'] = 'timeout'
        except (http.client.HTTPException, ValueError):
            record['fate'] = 'invalid-response'
        except OSError as error:
            record['fate'] = f'oserror:{error.errno}'
        record['completed'] = time.monotonic() - origin
        return record


def sample(request, count, interval, origin=None):
    origin = time.monotonic() if origin is None else origin
    with ThreadPoolExecutor(max_workers=count) as pool:
        pending = []
        start = time.monotonic()
        for index in range(count):
            scheduled = start - origin + index * interval
            time.sleep(max(0, origin + scheduled - time.monotonic()))
            pending.append(pool.submit(request, origin, scheduled))
        return [future.result() for future in pending]


def summarize(records):
    return {'affected': sum(record['fate'] != 'served-fast' for record in records),
            'fates': dict(Counter(record['fate'] for record in records)),
            'records': records}


@contextmanager
def managed_process(argv, directory, env, log_path):
    with log_path.open('w') as log:
        process = subprocess.Popen(argv, cwd=directory, env=env, stdout=log,
                                   stderr=subprocess.STDOUT, start_new_session=True)
    try:
        yield process
    finally:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass
        # The master may have exited while descendants still own its group.
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait(timeout=5)


def wait_for(client, process, generation, budget):
    deadline = time.monotonic() + budget
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f'server exited with status {process.returncode}')
        if client.probe(time.monotonic()).get('generation') == generation:
            return
        time.sleep(0.1)
    raise RuntimeError(f'no complete {generation} response within {budget}s')


def app_source(binding, park, reload, generation):
    return f'''import asyncio
from blackbull import BlackBull
app = BlackBull()
@app.route(path='/ready')
async def ready():
    return {generation.encode()!r}
@app.on_startup
async def park():
    await asyncio.sleep({park!r})
if __name__ == '__main__':
    app.run({binding}, workers=1, reload={reload!r},
            reload_paths=[__file__])
'''


def watch(sock, client, stop, origin, active):
    try:
        while not stop.is_set():
            if active:
                exchange(sock, client.timeout)
                stop.wait(0.2)
            elif not sock.recv(1):
                return {'fate': 'stopped' if stop.is_set() else 'eof',
                        't': time.monotonic() - origin}
        return {'fate': 'stopped', 't': time.monotonic() - origin}
    except TimeoutError:
        return {'fate': 'timeout', 't': time.monotonic() - origin}
    except (OSError, http.client.HTTPException, ValueError) as error:
        return {'fate': 'stopped' if stop.is_set() else type(error).__name__,
                't': time.monotonic() - origin}


def measure(args, directory, output):
    if args.family == 'unix':
        address = str(directory / 'server.sock')
        family = socket.AF_UNIX
        binding = f'unix_path={address!r}'
    else:
        family = socket.AF_INET6 if args.family == 'inet6' else socket.AF_INET
        host = '::1' if args.family == 'inet6' else '127.0.0.1'
        with socket.socket(family, socket.SOCK_STREAM) as reservation:
            reservation.bind((host, 0))
            port = reservation.getsockname()[1]
        address = (host, port)
        binding = f'port={port}'
    client = Client(family, address, args.client_timeout, args.slow)
    script = directory / 'run_server.py'
    script.write_text(app_source(binding, args.park, args.reload, 'v1'))
    env = dict(os.environ, PYTHONPATH=str(args.source), PYTHONUNBUFFERED='1',
               WATCHFILES_FORCE_POLLING='1', WATCHFILES_POLL_DELAY_MS='100',
               BB_ACCESS_LOG='0', BB_SOCKET_BACKLOG=str(args.backlog))
    origin = time.monotonic()
    with managed_process([sys.executable, str(script)], directory, env,
                         directory / 'server.log') as process:
        output['master_pid'] = process.pid
        output['cold'] = summarize(sample(client.probe, args.requests, args.interval, origin))
        wait_for(client, process, 'v1', args.budget)
        output['control'] = summarize(sample(client.probe, args.requests, args.interval))
        if output['control']['affected']:
            raise RuntimeError('steady-state control failed; measurement is invalid')
        if not args.reload:
            return
        # Let the polling watcher finish its baseline before rewriting the file.
        time.sleep(1.5)
        held, pending = [], []
        stop = threading.Event()
        with ThreadPoolExecutor(max_workers=max(1, args.warm)) as pool:
            try:
                for _ in range(args.warm):
                    sock = client.connect()
                    held.append(sock)
                    if exchange(sock, client.timeout) != 'v1':
                        raise RuntimeError('held connection did not serve the old generation')
                origin = time.monotonic()
                for sock in held:
                    pending.append(pool.submit(watch, sock, client, stop, origin,
                                               args.active_warm))
                script.write_text(app_source(binding, args.park, True, 'v2'))
                output['reload'] = summarize(sample(client.probe, args.requests,
                                                   args.interval, origin))
                wait_for(client, process, 'v2', args.budget)
                output['new_generation_verified_after_s'] = time.monotonic() - origin
            finally:
                stop.set()
                for sock in held:
                    shutdown(sock)
                    sock.close()
                output['held'] = [future.result() for future in pending]


def arguments(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument('--family', choices=('inet', 'inet6', 'unix'), default='inet')
    parser.add_argument('--reload', action='store_true')
    parser.add_argument('--active-warm', action='store_true')
    parser.add_argument('--warm', type=int, default=5)
    parser.add_argument('--requests', type=int, default=40)
    parser.add_argument('--interval', type=float, default=0)
    parser.add_argument('--park', type=float, default=6)
    parser.add_argument('--backlog', type=int, default=1024)
    parser.add_argument('--client-timeout', type=float, default=30)
    parser.add_argument('--slow', type=float, default=0.5)
    parser.add_argument('--budget', type=float, default=45)
    args = parser.parse_args(argv)
    for name in ('interval', 'park', 'client_timeout', 'slow', 'budget'):
        value = getattr(args, name)
        if not math.isfinite(value) or value < 0 or (name not in ('interval', 'park') and value == 0):
            parser.error(f'{name.replace("_", "-")} has an invalid value')
    if not 1 <= args.requests <= 256 or not 0 <= args.warm <= 256 or args.backlog < 1:
        parser.error('requests must be 1..256; warm 0..256; backlog positive')
    if args.active_warm and (not args.reload or not args.warm):
        parser.error('active-warm requires reload and at least one warm connection')
    args.source = args.source.resolve()
    if not (args.source / 'blackbull').is_dir():
        parser.error('source must be a BlackBull checkout')
    return args


def main(argv=None):
    args = arguments(argv)
    output = {'valid': False, 'configuration': {**vars(args), 'source': str(args.source)}}
    with tempfile.TemporaryDirectory(prefix='blackbull-reload-') as temporary:
        directory = Path(temporary)
        try:
            measure(args, directory, output)
            output['valid'] = True
        except (OSError, RuntimeError, http.client.HTTPException, ValueError) as error:
            output['error'] = str(error)
        finally:
            log = directory / 'server.log'
            if log.exists():
                with log.open('rb') as stream:
                    stream.seek(max(0, log.stat().st_size - 65536))
                    output['server_log_tail'] = stream.read().decode(errors='replace')
    print(json.dumps(output, indent=2))
    return 0 if output['valid'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
