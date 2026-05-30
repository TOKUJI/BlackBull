"""Spawn cleartext + TLS BlackBull workers per HttpArena container spec.

HttpArena expects each framework container to expose:
  :8080  HTTP/1.1 cleartext + h2c (prior-knowledge, same port — BlackBull
         autodetects HTTP/2 from the connection preface)
  :8081  HTTPS — TLS terminating HTTP/1.1 and HTTP/2 via ALPN

Sidecar mounts:
  /data/dataset.json    read-only dataset (50 items)
  /data/static/         read-only static assets (not served yet)
  /certs/server.crt
  /certs/server.key

This launcher follows the same shape as the fastapi reference launcher:
two ``app.py`` subprocesses, both terminated on SIGTERM/SIGINT.
"""
import os
import signal
import subprocess
import sys
import time


HTTP_PORT  = int(os.environ.get('HTTPARENA_HTTP_PORT',  '8080'))
HTTPS_PORT = int(os.environ.get('HTTPARENA_HTTPS_PORT', '8081'))
TLS_CERT   = os.environ.get('TLS_CERT', '/certs/server.crt')
TLS_KEY    = os.environ.get('TLS_KEY',  '/certs/server.key')

PY = sys.executable
APP = os.path.join(os.path.dirname(__file__), 'app.py')


def _spawn(extra):
    return subprocess.Popen([PY, APP, *extra])


http_proc  = _spawn(['--port', str(HTTP_PORT)])
https_proc = None
if os.path.exists(TLS_CERT) and os.path.exists(TLS_KEY):
    https_proc = _spawn(['--port', str(HTTPS_PORT),
                         '--cert', TLS_CERT, '--key', TLS_KEY])
else:
    sys.stderr.write(
        f'launcher: TLS cert/key not present at {TLS_CERT} / {TLS_KEY}; '
        f'starting cleartext only\n')


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
