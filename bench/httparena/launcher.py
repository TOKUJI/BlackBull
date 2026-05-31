"""Spawn cleartext + TLS BlackBull workers per HttpArena container spec.

HttpArena expects each framework container to expose:
  :8080  HTTP/1.1 cleartext + h2c (prior-knowledge, same port — BlackBull
         autodetects HTTP/2 from the connection preface)
  :8443  HTTPS — TLS terminating HTTP/1.1 and HTTP/2 via ALPN

Sidecar mounts:
  /data/dataset.json    read-only dataset (50 items)
  /data/static/         read-only static assets (not served yet)
  /certs/server.crt
  /certs/server.key

The launcher follows the same shape as the fastapi reference launcher:
two ``app.py`` subprocesses, both terminated on SIGTERM/SIGINT.

Sprint 28 Task 4: HTTPS moved from :8081 to :8443 to match HttpArena's
documented port contract (Task 3 EC2 validate surfaced this — the
`baseline-h2` and `static-h2` profiles probe :8443 specifically).
The h2c-only :8082 binding is NOT provided; we don't subscribe to
the `baseline-h2c` / `json-h2c` profiles in meta.json because they
require :8082 to refuse HTTP/1.1, which BlackBull's plaintext port
doesn't currently support (it autodetects both H/1.1 and h2c on the
same socket).
"""
import os
import signal
import socket
import subprocess
import sys
import time


HTTP_PORT  = int(os.environ.get('HTTPARENA_HTTP_PORT',  '8080'))
HTTPS_PORT = int(os.environ.get('HTTPARENA_HTTPS_PORT', '8443'))
TLS_CERT   = os.environ.get('TLS_CERT', '/certs/server.crt')
TLS_KEY    = os.environ.get('TLS_KEY',  '/certs/server.key')

# Worker count.  HTTPARENA_WORKERS=0 → cpu_count (matches the fastapi
# reference launcher, which auto-scales by sched_getaffinity).  Default
# to cpu_count for apples-to-apples vs peer launchers; override down to
# 1 with HTTPARENA_WORKERS=1 for per-process measurements.  Sprint 28
# Task 4 finding: the first EC2 run used the historical default of 1
# worker, putting BlackBull at a 4× worker disadvantage vs FastAPI's
# uvicorn-default cpu_count and producing misleadingly weak static
# numbers (22 r/s vs FastAPI 1281 r/s).
_workers_env = os.environ.get('HTTPARENA_WORKERS', '0')
try:
    WORKERS = int(_workers_env)
except ValueError:
    WORKERS = 0
if WORKERS == 0:
    try:
        WORKERS = max(len(os.sched_getaffinity(0)), 1)
    except (AttributeError, OSError):
        WORKERS = os.cpu_count() or 1

PY = sys.executable
APP = os.path.join(os.path.dirname(__file__), 'app.py')


def _spawn(extra):
    return subprocess.Popen([PY, APP, *extra, '--workers', str(WORKERS)])


def _wait_for_port(port: int, timeout: float = 20.0) -> bool:
    """Block until TCP port is accepting connections, or timeout.

    HttpArena's per-profile test scripts don't all wait for HTTPS to
    be up before probing it (the json-tls validate-script doesn't
    have a [wait] step like baseline-h2's does).  So we ensure both
    listeners are bound before the launcher exits its setup phase.
    Sprint 28 Task 4 finding.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(('127.0.0.1', port), timeout=1.0):
                return True
        except (ConnectionRefusedError, OSError):
            time.sleep(0.1)
    return False


http_proc  = _spawn(['--port', str(HTTP_PORT)])
https_proc = None
if os.path.exists(TLS_CERT) and os.path.exists(TLS_KEY):
    https_proc = _spawn(['--port', str(HTTPS_PORT),
                         '--cert', TLS_CERT, '--key', TLS_KEY])
else:
    sys.stderr.write(
        f'launcher: TLS cert/key not present at {TLS_CERT} / {TLS_KEY}; '
        f'starting cleartext only\n')

# Block until both ports are ready before going into wait() mode.
# Prevents the json-tls race where the HTTPS test probes :8443 before
# the second subprocess has finished binding.
if not _wait_for_port(HTTP_PORT):
    sys.stderr.write(f'launcher: HTTP port {HTTP_PORT} did not bind within 20s\n')
if https_proc is not None and not _wait_for_port(HTTPS_PORT):
    sys.stderr.write(f'launcher: HTTPS port {HTTPS_PORT} did not bind within 20s\n')


def _shutdown(*_):
    for p in (http_proc, https_proc):
        if p is not None:
            p.terminate()
    time.sleep(1)
    for p in (http_proc, https_proc):
        if p is not None and p.poll() is None:
            p.kill()
    sys.exit(0)


signal.signal(signal.SIGTERM, _shutdown)
signal.signal(signal.SIGINT,  _shutdown)

try:
    rc = http_proc.wait()
finally:
    if https_proc is not None:
        https_proc.terminate()
sys.exit(rc)
