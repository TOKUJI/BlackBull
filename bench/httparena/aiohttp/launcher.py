"""aiohttp launcher for HttpArena — N worker processes, one SO_REUSEPORT socket each.

HttpArena expects:
  :8080  HTTP/1.1 cleartext  (baseline profile)

aiohttp has no built-in multiprocess worker, and gunicorn's --reuse-port does
NOT give the aiohttp worker per-process listening sockets (gunicorn 23.0.0:
only the master's shared pre-fork socket ends up listening; the workers' new
sockets never reach the LISTEN state).  So this launcher spawns WEB_WORKERS
subprocesses, each running app.py with --reuse-port (aiohttp
web.run_app(reuse_port=True)) — every worker owns its own SO_REUSEPORT
listener and the kernel load-balances connections across them.  This is what
lets aiohttp scale past the shared-socket accept bottleneck at high
connection counts (the HttpArena 512/4096c baseline).

Worker count is WEB_WORKERS (default: nproc inside the container), matching
how the sanic and BlackBull launchers size their workers.

Forwarded signals terminate all workers; the launcher keeps running until
they are gone.
"""
import multiprocessing
import os
import signal
import subprocess
import sys
import time

APP = os.path.join(os.path.dirname(__file__), "app.py")
WORKERS = int(os.environ.get("WEB_WORKERS", str(multiprocessing.cpu_count())))

PROCS: list[subprocess.Popen] = []


def _shutdown(signum, frame):
    print(f"[launcher] received signal {signum}, shutting down ...")
    for p in PROCS:
        p.terminate()
    for p in PROCS:
        try:
            p.wait(timeout=10)
        except subprocess.TimeoutExpired:
            p.kill()
    sys.exit(0)


def main():
    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    print(f"[launcher] aiohttp HttpArena — workers={WORKERS} (SO_REUSEPORT)")

    cmd = [sys.executable, APP, "--reuse-port"]
    for i in range(WORKERS):
        proc = subprocess.Popen(cmd)
        PROCS.append(proc)
        print(f"[launcher] spawned worker {i} pid={proc.pid} port=8080")

    # Keep alive; reap exit codes.  If a worker dies the others keep serving.
    while PROCS:
        for p in list(PROCS):
            ret = p.poll()
            if ret is not None:
                print(f"[launcher] worker pid={p.pid} exited with {ret}")
                PROCS.remove(p)
        time.sleep(1)


if __name__ == "__main__":
    main()
