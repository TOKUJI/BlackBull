"""Multi-worker master process for BlackBull.

Uses a pre-fork model:
  1. The master binds sockets once (via ASGIServer.open_socket).
  2. N worker processes are forked; each inherits the socket file descriptors.
  3. The master monitors workers in a synchronous loop and respawns any that crash.
  4. On SIGTERM or SIGINT the master sends SIGTERM to all workers, waits up to
     *shutdown_timeout* seconds, then SIGKILLs any that are still alive.

When ``reload=True`` the master additionally runs a file watcher; on any
matching change it SIGTERMs the workers, marks the listening sockets
inheritable, and ``os.execvp``\\ s itself with the original argv — the
fresh process adopts the inherited fds and re-forks workers from the
new code.  See :mod:`blackbull.server.reload`.

The worker entry point is :func:`blackbull.server.worker.run_worker`.
Each worker runs its own asyncio event loop and its own ASGI lifespan cycle.

Usage::

    from blackbull.server.multiworker import MultiWorkerServer

    server = MultiWorkerServer(app, raw_sockets, ssl_context, workers=4)
    server.run()          # blocks until SIGTERM / SIGINT
"""
import logging
import multiprocessing
import os
import signal
import time

from .worker import run_worker
from ..protocol.rsock import create_dual_stack_sockets, REUSEPORT_SUPPORTED

logger = logging.getLogger(__name__)

_MONITOR_INTERVAL = 1.0   # seconds between worker health checks
_SHUTDOWN_TIMEOUT = 10.0  # seconds to wait for graceful shutdown before SIGKILL
_RELOAD_TICK = 0.1        # finer poll when reload mode is active


class MultiWorkerServer:
    """Spawns and supervises N worker processes.

    Parameters
    ----------
    app:
        ASGI application callable.
    raw_sockets:
        Pre-bound sockets that every worker will inherit.
    ssl_context:
        TLS context, or None for plain HTTP.
    workers:
        Number of worker processes to maintain.
    max_connections:
        Per-worker connection limit forwarded to :class:`ASGIServer`.
    shutdown_timeout:
        Seconds to wait for graceful shutdown before sending SIGKILL.
    """

    def __init__(self, app, raw_sockets, ssl_context, *,
                 workers: int,
                 max_connections: int = 500,
                 stream_queue_depth: int = 64,
                 ws_queue_depth: int = 256,
                 shutdown_timeout: float = _SHUTDOWN_TIMEOUT,
                 reload: bool = False,
                 reload_paths=None,
                 protocol_sockets=None):
        if workers < 1:
            raise ValueError(f'workers must be >= 1, got {workers}')
        self._app = app
        self._ssl_context = ssl_context
        self._num_workers = workers
        # Stateful non-ASGI protocol listeners (eg MQTT), bound once by the
        # master.  HTTP scales across every worker, but a stateful broker must
        # have a single owner, so these go to worker 0 only — see
        # ``_spawn_worker``.  The master keeps them open for the worker's
        # lifetime so a respawned worker 0 re-inherits them.
        self._protocol_sockets = list(protocol_sockets or [])
        self._max_connections = max_connections
        self._stream_queue_depth = stream_queue_depth
        self._ws_queue_depth = ws_queue_depth
        self._shutdown_timeout = shutdown_timeout
        self._reload = reload
        self._reload_paths = reload_paths
        self._reload_pending = False
        self._watcher = None  # set in run() when reload is enabled
        self._processes: list = []
        # The original listening sockets the master adopts/binds.  These
        # are the ones we hand off across exec when reloading — distinct
        # from ``_worker_sockets`` which may be SO_REUSEPORT sets.
        self._listening_sockets = list(raw_sockets)
        # Use 'fork' so workers inherit socket FDs and the app object without
        # pickling.  'spawn' would require the app and sockets to be picklable
        # and would re-import all modules from scratch.
        self._mp_ctx = multiprocessing.get_context('fork')

        # Per-worker sockets via SO_REUSEPORT: each worker gets its own kernel
        # accept queue so the kernel distributes connections evenly without
        # thundering-herd.  Falls back to the shared inherited socket when
        # SO_REUSEPORT is unavailable or disabled via BB_SOCKET_REUSEPORT=0.
        # Reload mode forces the shared-socket path: the master must hold
        # the listening sockets so they survive worker recycling and exec.
        from ..env import get_settings as _get_settings  # noqa: PLC0415
        cfg = _get_settings()
        if workers > 1 and REUSEPORT_SUPPORTED and cfg.socket_reuseport and not reload:
            port = raw_sockets[0].getsockname()[1]
            # Close the master sockets BEFORE creating worker sockets.
            # Linux requires every socket co-binding on a port to have SO_REUSEPORT set;
            # the master's sockets were bound without it so they must be gone first.
            for s in raw_sockets:
                s.close()
            self._listening_sockets = []  # master no longer holds listeners
            self._worker_sockets = [
                create_dual_stack_sockets(port, backlog=cfg.socket_backlog, reuseport=True)
                for _ in range(workers)
            ]
            logger.info('SO_REUSEPORT: created %d per-worker socket set(s) on port %d',
                        workers, port)
        else:
            # Single-worker, SO_REUSEPORT unavailable, or reload mode:
            # all workers share the master's pre-bound sockets so the
            # master can hand them off across reload.
            self._worker_sockets = [raw_sockets] * workers

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def run(self) -> None:
        """Spawn workers and block until a shutdown signal is received.

        When ``reload`` is enabled the master also runs a file watcher and
        re-execs itself on any matching change — see
        :mod:`blackbull.server.reload`.
        """
        self._install_signal_handlers()
        self._spawn_all()

        logger.info(
            'Master (PID %d) running %d worker(s)%s',
            os.getpid(), self._num_workers,
            ' [auto-reload]' if self._reload else '',
        )

        if self._reload:
            self._start_watcher()

        self._stopped = False
        tick = _RELOAD_TICK if self._reload else _MONITOR_INTERVAL
        try:
            elapsed_since_reap = 0.0
            while not self._stopped:
                if self._reload and self._reload_pending:
                    logger.info('reload: change detected — recycling workers')
                    self._reload_pending = False
                    self._reload_now()  # does not return — execvp's
                # Only health-check on _MONITOR_INTERVAL cadence even when
                # the loop ticks fast for reload responsiveness.
                if elapsed_since_reap >= _MONITOR_INTERVAL:
                    self._reap_and_respawn()
                    elapsed_since_reap = 0.0
                time.sleep(tick)
                elapsed_since_reap += tick
        finally:
            if self._watcher is not None:
                self._watcher.stop()
            self._shutdown_all()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _spawn_worker(self, worker_id: int):
        # Only worker 0 owns the stateful protocol listeners (MQTT, …); the
        # rest serve HTTP only.  Worker 0 inherits the master's still-open
        # protocol fds via fork, so a respawn after a crash re-adopts them
        # and the broker resumes on the same port.
        protocol_sockets = self._protocol_sockets if worker_id == 0 else None
        p = self._mp_ctx.Process(
            target=run_worker,
            args=(self._app, self._worker_sockets[worker_id], self._ssl_context,
                  worker_id, self._max_connections,
                  self._stream_queue_depth, self._ws_queue_depth,
                  protocol_sockets),
            daemon=False,  # workers must be reaped explicitly on shutdown
            name=f'bb-worker-{worker_id}',
        )
        p.start()
        logger.info('Spawned worker %d (PID %d)', worker_id, p.pid)
        return p

    def _spawn_all(self) -> None:
        self._processes = [self._spawn_worker(i) for i in range(self._num_workers)]

    def _reap_and_respawn(self) -> None:
        for i, p in enumerate(self._processes):
            if not p.is_alive():
                logger.warning(
                    'Worker %d (PID %d) exited with code %s — respawning',
                    i, p.pid, p.exitcode,
                )
                p.close()
                self._processes[i] = self._spawn_worker(i)

    def _shutdown_all(self) -> None:
        logger.info('Sending SIGTERM to %d worker(s)', len(self._processes))
        for p in self._processes:
            if p.is_alive():
                p.terminate()

        deadline = time.monotonic() + self._shutdown_timeout
        for p in self._processes:
            remaining = max(0.0, deadline - time.monotonic())
            p.join(timeout=remaining)
            if p.is_alive():
                logger.warning('Worker PID %d did not stop — sending SIGKILL', p.pid)
                p.kill()
                p.join()
            p.close()

        self._processes.clear()
        logger.info('All workers stopped')

    def _install_signal_handlers(self) -> None:
        import threading  # noqa: PLC0415
        if threading.current_thread() is not threading.main_thread():
            logger.debug('Not in main thread — skipping signal handler installation')
            return

        def _handle_stop(signo, frame):  # noqa: ANN001
            logger.info('Master received signal %d — initiating shutdown', signo)
            self._stopped = True

        signal.signal(signal.SIGTERM, _handle_stop)
        signal.signal(signal.SIGINT, _handle_stop)

    # ------------------------------------------------------------------
    # Reload
    # ------------------------------------------------------------------

    def _start_watcher(self) -> None:
        from .reload import FileChangeWatcher  # noqa: PLC0415

        paths = self._reload_paths or [os.getcwd()]

        def _on_change() -> None:
            # Called on the watcher thread.  Setting a bool is atomic in
            # CPython; the main loop polls it on _RELOAD_TICK cadence.
            self._reload_pending = True

        self._watcher = FileChangeWatcher(paths, _on_change)
        self._watcher.start()

    def _reload_now(self) -> None:
        """SIGTERM workers, then re-exec the master.  Does not return."""
        from .reload import exec_self_with_sockets  # noqa: PLC0415

        # Stop the watcher first so the next-generation master can start
        # its own thread without conflict.
        if self._watcher is not None:
            self._watcher.stop()
            self._watcher = None

        self._shutdown_all()

        if not self._listening_sockets:
            # SO_REUSEPORT path closed the master sockets — reload was
            # supposed to be incompatible with that branch, so we should
            # never get here.  If we do, fall back to normal shutdown.
            logger.error('reload: master holds no listening sockets — cannot exec')
            self._stopped = True
            return

        # execvp replaces the process — control does not return.
        exec_self_with_sockets(self._listening_sockets)
