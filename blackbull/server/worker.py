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
               ws_queue_depth: int = 256,
               protocol_sockets=None) -> None:
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
    protocol_sockets:
        Pre-bound listener sets for stateful non-ASGI protocols (eg the MQTT
        broker), as ``[(socks, binding), …]``.  The master hands these to a
        single worker only (HTTP scales across all workers, but a stateful
        broker must have one owner), so this is non-empty for that worker and
        ``None`` for the rest.
    """
    # Workers should not respond to Ctrl+C directly — the master handles the
    # signal and sends SIGTERM to every worker for a coordinated shutdown.
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    # ``fork`` inherits any SIGTERM handler the master installed (eg the
    # one that flips ``_stopped`` to break the supervision loop).  That
    # handler is a no-op inside the worker — it does not stop the
    # asyncio loop — and prevents the default-terminate behaviour from
    # firing.  Restore the default disposition so master.terminate()
    # actually exits the worker promptly; without this the master has
    # to SIGKILL every recycle, which makes auto-reload glacial.
    signal.signal(signal.SIGTERM, signal.SIG_DFL)

    from ..env import apply_event_loop_policy, get_settings as _get_settings  # noqa: PLC0415
    from ..logger import setup_async_logging, teardown_async_logging  # noqa: PLC0415
    from .server import ASGIServer  # noqa: PLC0415 — deferred to avoid import cycles

    cfg = _get_settings()
    apply_event_loop_policy(cfg)
    if cfg.async_logging:
        setup_async_logging(
            log_format=cfg.log_format,
            syslog_addr=cfg.log_syslog_addr,
            batch_size=cfg.log_batch_size,
            batch_timeout_ms=cfg.log_batch_timeout_ms,
        )
    if not cfg.access_log:
        logging.getLogger('blackbull.access').setLevel(logging.WARNING)

    # Pin this worker to a specific CPU core to improve L1/L2 cache locality
    # and reduce context switching.  Only attempted on Linux (sched_setaffinity).
    if hasattr(os, 'sched_setaffinity'):
        cpu_count = os.cpu_count() or 1
        cpu = worker_id % cpu_count
        try:
            os.sched_setaffinity(0, {cpu})
            logger.debug('Worker %d pinned to CPU %d', worker_id, cpu)
        except OSError as exc:
            logger.debug('CPU affinity pinning unavailable: %s', exc)

    server = ASGIServer(app, ssl_context=ssl_context, max_connections=max_connections,
                        stream_queue_depth=stream_queue_depth,
                        ws_queue_depth=ws_queue_depth)
    # Inject the inherited sockets so ASGIServer.run() skips its own bind step.
    server.raw_sockets = raw_sockets
    server.port = raw_sockets[0].getsockname()[1] if raw_sockets else 0

    # Adopt the stateful-protocol listeners (MQTT, …) if this is the worker the
    # master designated to own them.  ASGIServer.run() serves whatever is in
    # ``_protocol_sockets`` alongside the HTTP listener; an empty list (the
    # other workers) just means HTTP-only.
    if protocol_sockets:
        server._protocol_sockets = list(protocol_sockets)
        server.protocol_ports = {
            binding.name: socks[0].getsockname()[1]
            for socks, binding in protocol_sockets if socks
        }
        logger.info('Worker %d owns %d stateful protocol listener(s)',
                    worker_id, len(protocol_sockets))

    logger.info('Worker %d starting (PID %d)', worker_id, os.getpid())
    try:
        asyncio.run(server.run())
    except KeyboardInterrupt:
        pass  # SIGINT is ignored, but guard against any race
    finally:
        logger.info('Worker %d exiting (PID %d)', worker_id, os.getpid())
        teardown_async_logging()
