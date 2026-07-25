"""Sanic launcher for HttpArena — spawns Sanic on required ports.

HttpArena expects:
  :8080  HTTP/1.1 cleartext
  :8081  HTTPS HTTP/1.1  (json-tls profile)
  :8082  h2c             — Sanic does NOT support; left unbound
  :8443  HTTPS H2        — Sanic does NOT support; serves HTTP/1.1

Sanic workers are set via WEB_WORKERS env var.
Cert/key come from /certs/ (HttpArena's standard mount).

Parallel subprocess.Popen per port, all terminated on SIGTERM/SIGINT.
"""
import multiprocessing
import os
import signal
import subprocess
import sys
import time

APP = os.path.join(os.path.dirname(__file__), "app.py")
CERT = os.environ.get("CERT_PATH", "/certs/server.crt")
KEY = os.environ.get("KEY_PATH", "/certs/server.key")
WORKERS = int(os.environ.get("WEB_WORKERS", str(multiprocessing.cpu_count())))

PROCS: list[subprocess.Popen] = []


def _spawn(port: int, tls: bool = False):
    cmd = [
        sys.executable, APP,
        "--host", "0.0.0.0",
        "--port", str(port),
        "--workers", str(WORKERS),
    ]
    if tls and os.path.exists(CERT) and os.path.exists(KEY):
        cmd += ["--cert", CERT, "--key", KEY]
    proc = subprocess.Popen(cmd)
    PROCS.append(proc)
    print(f"[launcher] spawned pid={proc.pid} port={port} tls={tls}")
    return proc


def _shutdown(signum, frame):
    print(f"[launcher] received signal {signum}, shutting down ...")
    for p in PROCS:
        p.terminate()
    for p in PROCS:
        try:
            p.wait(timeout=5)
        except subprocess.TimeoutExpired:
            p.kill()
    sys.exit(0)


def main():
    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    print(f"[launcher] Sanic HttpArena — workers={WORKERS}")

    # Port 8080: HTTP/1.1 cleartext
    _spawn(8080, tls=False)

    # Port 8081: HTTPS HTTP/1.1
    _spawn(8081, tls=True)

    # Port 8443: HTTPS (HTTP/1.1 — Sanic has no H2)
    _spawn(8443, tls=True)

    # Keep alive
    while PROCS:
        for p in list(PROCS):
            ret = p.poll()
            if ret is not None:
                print(f"[launcher] pid={p.pid} exited with {ret}")
                PROCS.remove(p)
        time.sleep(1)


if __name__ == "__main__":
    main()
