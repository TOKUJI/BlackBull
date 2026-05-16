"""Worker process entry point for multi-worker deployments.

Each worker is a forked child that inherits pre-bound socket file descriptors
from the master, creates a fresh asyncio event loop, and runs a standard
ASGIServer with those sockets.

The worker:
  - ignores SIGINT (the master handles Ctrl+C and sends SIGTERM to workers)
  - runs lifespan independently (startup/shutdown per worker)
  - tracks active connections via the ASGIServer connection counter
"""
import asyncio
import logging
import os
import signal

logger = logging.getLogger(__name__)


def run_worker(app, raw_sockets, ssl_context, worker_id: int,
               max_connections: int,
               stream_queue_depth: int = 64,
               ws_queue_depth: int = 256) -> None:
    """Entry point executed in each worker process.

    Parameters
    ----------
    app:
        The ASGI application callable (a BlackBull instance or any ASGI app).
    raw_sockets:
        Pre-bound socket objects inherited from the master via fork.
    ssl_context:
        TLS context to pass to asyncio.start_server, or None for plain HTTP.
    worker_id:
        Zero-based index used only for logging.
    max_connections:
        Per-worker connection limit; passed to ASGIServer.
    """
    # Workers should not respond to Ctrl+C directly — the master handles the
    # signal and sends SIGTERM to every worker for a coordinated shutdown.
    signal.signal(signal.SIGINT, signal.SIG_IGN)

    from .server import ASGIServer  # noqa: PLC0415 — deferred to avoid import cycles

    server = ASGIServer(app, ssl_context=ssl_context, max_connections=max_connections,
                        stream_queue_depth=stream_queue_depth,
                        ws_queue_depth=ws_queue_depth)
    # Inject the inherited sockets so ASGIServer.run() skips its own bind step.
    server.raw_sockets = raw_sockets
    server.port = raw_sockets[0].getsockname()[1] if raw_sockets else 0

    logger.info('Worker %d starting (PID %d)', worker_id, os.getpid())
    try:
        asyncio.run(server.run())
    except KeyboardInterrupt:
        pass  # SIGINT is ignored, but guard against any race
    logger.info('Worker %d exiting (PID %d)', worker_id, os.getpid())
