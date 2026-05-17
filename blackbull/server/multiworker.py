"""Multi-worker master process for BlackBull.

Uses a pre-fork model:
  1. The master binds sockets once (via ASGIServer.open_socket).
  2. N worker processes are forked; each inherits the socket file descriptors.
  3. The master monitors workers in a synchronous loop and respawns any that crash.
  4. On SIGTERM or SIGINT the master sends SIGTERM to all workers, waits up to
     *shutdown_timeout* seconds, then SIGKILLs any that are still alive.

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
                 shutdown_timeout: float = _SHUTDOWN_TIMEOUT):
        if workers < 1:
            raise ValueError(f'workers must be >= 1, got {workers}')
        self._app = app
        self._ssl_context = ssl_context
        self._num_workers = workers
        self._max_connections = max_connections
        self._stream_queue_depth = stream_queue_depth
        self._ws_queue_depth = ws_queue_depth
        self._shutdown_timeout = shutdown_timeout
        self._processes: list = []
        # Use 'fork' so workers inherit socket FDs and the app object without
        # pickling.  'spawn' would require the app and sockets to be picklable
        # and would re-import all modules from scratch.
        self._mp_ctx = multiprocessing.get_context('fork')

        # Per-worker sockets via SO_REUSEPORT: each worker gets its own kernel
        # accept queue so the kernel distributes connections evenly without
        # thundering-herd.  Falls back to the shared inherited socket when
        # SO_REUSEPORT is unavailable or disabled via BB_SOCKET_REUSEPORT=0.
        from ..env import get_settings as _get_settings  # noqa: PLC0415
        cfg = _get_settings()
        if workers > 1 and REUSEPORT_SUPPORTED and cfg.socket_reuseport:
            port = raw_sockets[0].getsockname()[1]
            # Close the master sockets BEFORE creating worker sockets.
            # Linux requires every socket co-binding on a port to have SO_REUSEPORT set;
            # the master's sockets were bound without it so they must be gone first.
            for s in raw_sockets:
                s.close()
            self._worker_sockets = [
                create_dual_stack_sockets(port, backlog=cfg.socket_backlog, reuseport=True)
                for _ in range(workers)
            ]
            logger.info('SO_REUSEPORT: created %d per-worker socket set(s) on port %d',
                        workers, port)
        else:
            # Single-worker or SO_REUSEPORT unavailable: all workers share the
            # master's pre-bound sockets (original behaviour).
            self._worker_sockets = [raw_sockets] * workers

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def run(self) -> None:
        """Spawn workers and block until a shutdown signal is received."""
        self._install_signal_handlers()
        self._spawn_all()

        logger.info(
            'Master (PID %d) running %d worker(s)',
            os.getpid(), self._num_workers,
        )

        self._stopped = False
        try:
            while not self._stopped:
                self._reap_and_respawn()
                time.sleep(_MONITOR_INTERVAL)
        finally:
            self._shutdown_all()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _spawn_worker(self, worker_id: int):
        p = self._mp_ctx.Process(
            target=run_worker,
            args=(self._app, self._worker_sockets[worker_id], self._ssl_context,
                  worker_id, self._max_connections,
                  self._stream_queue_depth, self._ws_queue_depth),
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
