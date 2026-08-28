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

from .affinity import apply_worker_affinity, make_offload_executor
from .listener import HTTP
from .recipient import _WS_READ_INLINE

logger = logging.getLogger(__name__)


def run_worker(app, bound_listeners, ssl_context, worker_id: int,
               max_connections: int,
               stream_queue_depth: int = 64,
               ws_queue_depth: int = _WS_READ_INLINE) -> None:
    """Entry point executed in each worker process.

    Parameters
    ----------
    app:
        The ASGI application callable (a BlackBull instance or any ASGI app).
    bound_listeners:
        ``[(Listener, [socket, ...]), ...]`` inherited from the master via
        fork — every listener this worker owns, which the master decided from
        each listener's own ``workers`` field.
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
    # The handler inherited from the master is a no-op here that also
    # suppresses the default terminate.  SIG_DFL until ``_serve`` installs the
    # loop-stopping one below: a signal arriving before the loop exists has
    # nothing to stop.
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
            log_file=cfg.log_file,
        )
    if not cfg.access_log:
        logging.getLogger('blackbull.access').setLevel(logging.WARNING)

    # Pin this worker's event loop to one core so its hot state (header line
    # table, HPACK tables) stays cache-resident.  Deliberately after the
    # async-logging setup above: the log-drain thread is created there and
    # would otherwise inherit the pin.
    offload_mask = apply_worker_affinity(worker_id, cfg.cpu_pinning)

    server = ASGIServer(app, ssl_context=ssl_context, max_connections=max_connections,
                        stream_queue_depth=stream_queue_depth,
                        ws_queue_depth=ws_queue_depth)
    # Inject the inherited listeners so ASGIServer.run() skips its own bind
    # step.  Whether this worker owns a broker is already decided — it is in
    # the list or it is not.
    server.bound_listeners = list(bound_listeners)
    server._publish_socket_view()
    server.protocol_ports = {
        listener.speaks: socks[0].getsockname()[1]
        for listener, socks in bound_listeners
        if listener.speaks != HTTP and socks
    }
    if server.protocol_ports:
        logger.info('Worker %d owns %d single-owner listener(s)',
                    worker_id, len(server.protocol_ports))

    async def _serve() -> None:
        # Threads inherit the loop thread's affinity mask, so a pinned worker
        # would run every offloaded compression and file read on the one core
        # the loop is already saturating.  Hand the pool the mask the operator
        # gave us instead — offloading exists to get off this core.
        loop = asyncio.get_running_loop()
        if offload_mask is not None:
            loop.set_default_executor(make_offload_executor(offload_mask))

        def _drain_and_stop(*_):
            loop.create_task(server.stop(drain_timeout=cfg.worker_drain_timeout))

        try:
            # On the loop, not signal.signal, so the handler can await.
            loop.add_signal_handler(signal.SIGTERM, _drain_and_stop)
        except (NotImplementedError, RuntimeError):
            # No loop signal support; SIG_DFL above still applies.
            logger.debug('Worker %d: loop signal handler unavailable', worker_id)

        await server.run()

    logger.info('Worker %d starting (PID %d)', worker_id, os.getpid())
    try:
        asyncio.run(_serve())
    except KeyboardInterrupt:
        pass  # SIGINT is ignored, but guard against any race
    finally:
        logger.info('Worker %d exiting (PID %d)', worker_id, os.getpid())
        teardown_async_logging()
