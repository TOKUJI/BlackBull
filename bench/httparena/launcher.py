"""Spawn HTTP + HTTPS BlackBull workers per HttpArena framework spec.

HttpArena's `scripts/validate.sh` uses four ports:

  :8080  HTTP/1.1 cleartext (also serves h2c via prior-knowledge)
  :8081  HTTPS HTTP/1.1     — the ``json-tls`` profile probes here
  :8082  h2c only           — BlackBull's HTTP/2 preface auto-detect
                              handles h2c on the same app/workers as
                              :8080.  The dedicated :8082 listener is
                              opened so validate.sh's port-open probe
                              succeeds even when ``baseline-h2c`` /
                              ``json-h2c`` profiles aren't claimed in
                              ``meta.json``.
  :8443  HTTPS HTTP/2 ALPN  — ``baseline-h2`` / ``static-h2`` probe here

BlackBull claims both H/1.1+TLS and H/2+TLS, so :8081 and :8443 are
exposed simultaneously — same BlackBull, same cert, different
listeners.  ALPN negotiates HTTP/1.1 on :8081 connections and h2
on :8443.

Parallel ``subprocess.Popen`` per port, all terminated on SIGTERM /
SIGINT.  No port-readiness gating — BlackBull's bind happens in
``serve()`` as the first awaited step, so by the time the
subprocess is alive the kernel TCP accept queue is open.

BB_ACCESS_LOG
-------------
When the ``BB_ACCESS_LOG`` environment variable is set to a truthy
value (any non-empty string other than "0" / "false" / "no"), this
launcher attaches a ``StreamHandler`` to the ``blackbull.access``
logger that writes every request to **stderr** with a ``[ACCESS]`` prefix.

``bench/aws/httparena_compare.sh`` passes ``-e BB_ACCESS_LOG=1`` into the
container via the docker shim.  ``benchmark.sh``'s ``save_result()`` writes
``docker logs`` output to ``site/static/logs/<profile>/<conns>/blackbull.log``
before the container is removed.  ``httparena_compare.sh`` then greps
``[ACCESS]`` lines from that file to produce a standalone
``bb-access-blackbull-<profile>.log`` alongside the other result logs.

When ``BB_ACCESS_LOG`` is unset or falsy the FileHandler is not
added and per-request logging remains silenced (default: off).

BB_EARLY_BIND
-------------
When set to a truthy value, the launcher binds each listener's socket
(dual-stack, ``BB_SOCKET_BACKLOG``-deep) *itself*, before spawning the
``app.py`` child, and hands the already-listening fd to the child via
``BB_INHERIT_FDS`` + ``subprocess`` ``pass_fds``.  The child adopts it in
``open_socket`` (``adopt_inherited_sockets``) instead of binding.

This closes the cold-start gap: in the multi-worker path the child binds
only *after* importing blackbull, building the app, running the gRPC
warm-up (~3s) and forking workers, so for the first seconds after the
container starts nothing is listening — and HttpArena's ``_wait_grpc``
readiness gate is a no-op (``ghz`` exits 0 even on connection-refused), so
benchmark run 1 fires into that gap and every connection is refused
(run-1 collapse).  Pre-binding makes the kernel accept into the backlog
from t≈0, so the burst queues instead of being refused and is served once
the workers finish warming up.  See ``diagnostics/grpc-readiness-issue.md``
for the readiness-gate analysis.  Opt-in (default off) for A/B validation.
"""
import multiprocessing
import os
import signal
import socket
import subprocess
import sys
import time


HTTP_PORT      = int(os.environ.get('HTTPARENA_HTTP_PORT',      '8080'))
HTTPS_H1_PORT  = int(os.environ.get('HTTPARENA_HTTPS_H1_PORT',  '8081'))
H2C_PORT       = int(os.environ.get('HTTPARENA_H2C_PORT',       '8082'))
HTTPS_H2_PORT  = int(os.environ.get('HTTPARENA_HTTPS_H2_PORT',  '8443'))
TLS_CERT       = os.environ.get('TLS_CERT', '/certs/server.crt')
TLS_KEY        = os.environ.get('TLS_KEY',  '/certs/server.key')

# Worker count — WEB_WORKERS env var takes explicit precedence (set by
# the bench/aws/httparena_compare.sh shim via `docker run -e WEB_WORKERS=N`).
# Falls back to cgroup-aware CPU affinity (Linux sched_getaffinity), then
# multiprocessing.cpu_count().  Capped at 128 as a sanity guard.
_web_workers_env = os.environ.get('WEB_WORKERS', '').strip()
if _web_workers_env:
    try:
        WRK_COUNT = max(1, int(_web_workers_env))
    except ValueError:
        WRK_COUNT = None  # resolved below
else:
    WRK_COUNT = None

if WRK_COUNT is None:
    try:
        WRK_COUNT = min(len(os.sched_getaffinity(0)), 128)
    except (AttributeError, OSError):
        WRK_COUNT = multiprocessing.cpu_count()

# Log effective settings so they appear in `docker logs` output.
import resource as _resource
_soft_nofile, _hard_nofile = _resource.getrlimit(_resource.RLIMIT_NOFILE)
sys.stderr.write(
    f'launcher: workers={WRK_COUNT}'
    f'  nofile(soft)={_soft_nofile}'
    f'  nofile(hard)={_hard_nofile}\n'
)

# ---------------------------------------------------------------------------
# BB_ACCESS_LOG — per-request access log to stderr.
#
# Logging configuration is handled declaratively via logging_access.ini
# (standard Python logging.config.fileConfig format), NOT inline handler
# setup here.  When BB_ACCESS_LOG=1, httparena_compare.sh copies
# logging_access.ini into the container image alongside app.py.
# Each app.py worker process loads it at startup:
#
#   if os.path.isfile('logging_access.ini'):
#       logging.config.fileConfig('logging_access.ini')
#
# This launcher only echoes a status line so the setting is visible in
# `docker logs`.  No handler setup is performed here — Python logging
# state is NOT inherited across subprocess.Popen, so any setup in this
# process has no effect on the spawned app.py workers.
#
# Result: every completed request emits one [ACCESS] line to stderr,
# captured by `docker logs` → save_result() → blackbull.log.
# httparena_compare.sh greps [ACCESS] lines to produce bb-access-*.log.
# ---------------------------------------------------------------------------
_BB_ACCESS_LOG_ENV = os.environ.get('BB_ACCESS_LOG', '').strip().lower()
_ACCESS_LOG_ENABLED = _BB_ACCESS_LOG_ENV not in ('', '0', 'false', 'no')

if _ACCESS_LOG_ENABLED:
    sys.stderr.write('launcher: BB_ACCESS_LOG=1  access log → stderr (via logging_access.ini)\n')

PY = sys.executable
APP = os.path.join(os.path.dirname(__file__), 'app.py')

# Which listeners to spawn.  validate.sh requires all four;
# benchmark profiles only ever load one at a time, so disabling the
# idle three during a benchmark removes most of the worker-process
# scheduler pressure.  Comma-separated list of
# {"http","https-h1","h2c","https-h2"}; default = all four (so
# validate.sh's port-open probe finds :8082 listening).
_ports_env = os.environ.get('BB_HTTPARENA_PORTS', 'http,https-h1,h2c,https-h2')
_enabled_ports = {p.strip() for p in _ports_env.split(',') if p.strip()}

# ---------------------------------------------------------------------------
# BB_EARLY_BIND — pre-bind the listening socket in *this* launcher, before the
# heavy app.py child imports blackbull + builds the app + runs the gRPC warm-up
# + forks its workers, and hand the already-listening fd to the child.
#
# Why: in the multi-worker path the child binds its socket only AFTER
# ``run_warmup`` (BB_GRPC_WARMUP, ~3s) and the whole import/app-build, so for
# the first few seconds after the container starts NOTHING is listening on the
# port.  HttpArena's readiness gate (`_wait_grpc`) is a no-op — ghz exits 0 even
# against a refused connection — so the benchmark's run 1 fires into that gap
# and every connection is refused (run-1 collapse; runs 2-3 are clean).
#
# Pre-binding here means the kernel is accepting into the (deep) backlog from
# t≈0.  The burst queues instead of being refused, and the child's workers
# drain it once they finish warming up.  The child adopts the fd transparently
# via ``open_socket`` → ``adopt_inherited_sockets`` (BB_INHERIT_FDS), so app.py
# needs no change — it still calls ``app.run(port=…)`` and the bind is skipped.
#
# Opt-in (default off) so it can be A/B'd on EC2 before becoming the default.
# ---------------------------------------------------------------------------
_EARLY_BIND = os.environ.get('BB_EARLY_BIND', '').strip().lower() not in ('', '0', 'false', 'no')
_BACKLOG = int(os.environ.get('BB_SOCKET_BACKLOG', '1024') or 1024)

# Hold pre-bound sockets in the launcher so the fds stay open (and the listener
# stays alive across a child restart) until shutdown.
_HELD_SOCKETS: list[socket.socket] = []


def _prebind(port):
    """Bind + listen a dual-stack pair for *port* in the launcher process.

    Returns the list of inheritable fds (IPv4 first, then IPv6 when available)
    to hand to the child via ``pass_fds`` + ``BB_INHERIT_FDS``.  Mirrors
    ``blackbull.protocol.rsock.create_dual_stack_sockets`` (SO_REUSEADDR,
    IPV6_V6ONLY, deep backlog) but uses only the stdlib so the launcher stays
    free of the heavy blackbull import it is trying to front-run.
    """
    fds = []
    for family, host in ((socket.AF_INET, '0.0.0.0'), (socket.AF_INET6, '::')):
        try:
            s = socket.socket(family, socket.SOCK_STREAM)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            if family == socket.AF_INET6:
                s.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
            s.bind((host, port))
            s.listen(_BACKLOG)
            os.set_inheritable(s.fileno(), True)
            _HELD_SOCKETS.append(s)
            fds.append(s.fileno())
        except OSError as exc:
            sys.stderr.write(f'launcher: pre-bind {host}:{port} failed: {exc}\n')
    return fds


def _spawn(port, extra):
    argv = [PY, APP, '--port', str(port), *extra, '--workers', str(WRK_COUNT)]
    if not _EARLY_BIND:
        return subprocess.Popen(argv)
    fds = _prebind(port)
    if not fds:
        # Pre-bind failed entirely (port busy?) — fall back to letting the
        # child bind, rather than spawning a child that can never listen.
        sys.stderr.write(f'launcher: no pre-bound fds for port {port}; '
                         f'child will bind normally\n')
        return subprocess.Popen(argv)
    env = os.environ.copy()
    env['BB_INHERIT_FDS'] = ','.join(str(f) for f in fds)
    sys.stderr.write(f'launcher: pre-bound port {port} fds={fds} '
                     f'backlog={_BACKLOG} (early-bind)\n')
    return subprocess.Popen(argv, env=env, pass_fds=tuple(fds))


if _EARLY_BIND:
    sys.stderr.write(f'launcher: BB_EARLY_BIND=1  pre-binding listeners '
                     f'(backlog={_BACKLOG})\n')

http_proc       = _spawn(HTTP_PORT, []) if 'http' in _enabled_ports else None
h2c_proc        = _spawn(H2C_PORT, [])  if 'h2c'  in _enabled_ports else None
https_h1_proc   = None
https_h2_proc   = None
if os.path.exists(TLS_CERT) and os.path.exists(TLS_KEY):
    if 'https-h1' in _enabled_ports:
        https_h1_proc = _spawn(HTTPS_H1_PORT, ['--cert', TLS_CERT, '--key', TLS_KEY])
    if 'https-h2' in _enabled_ports:
        https_h2_proc = _spawn(HTTPS_H2_PORT, ['--cert', TLS_CERT, '--key', TLS_KEY])
else:
    sys.stderr.write(
        f'launcher: TLS cert/key not present at {TLS_CERT} / {TLS_KEY}; '
        f'starting cleartext only\n')


_PROCS = (http_proc, h2c_proc, https_h1_proc, https_h2_proc)


def _shutdown(*_):
    for p in _PROCS:
        if p is not None:
            p.terminate()
    time.sleep(1)
    for p in _PROCS:
        if p is not None and p.poll() is None:
            p.kill()
    sys.exit(0)


signal.signal(signal.SIGTERM, _shutdown)
signal.signal(signal.SIGINT,  _shutdown)

try:
    rc = http_proc.wait()
finally:
    for p in (h2c_proc, https_h1_proc, https_h2_proc):
        if p is not None:
            p.terminate()
sys.exit(rc)
