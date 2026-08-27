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
from .recipient import _WS_READ_INLINE

logger = logging.getLogger(__name__)


#: Seconds a worker spends letting accepted connections finish after SIGTERM.
#: Must stay inside ``MultiWorkerServer``'s ``shutdown_timeout`` (10 s) so the
#: wait ends in our own cancel rather than the master's SIGKILL.
_DRAIN_TIMEOUT = 8.0


def run_worker(app, raw_sockets, ssl_context, worker_id: int,
               max_connections: int,
               stream_queue_depth: int = 64,
               ws_queue_depth: int = _WS_READ_INLINE,
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
    # ``fork`` inherits the SIGTERM handler the master installed — the one
    # that flips ``_stopped`` to break the supervision loop, which inside a
    # worker is a no-op that also suppresses the default terminate.  The old
    # answer was ``SIG_DFL``: the worker died where it stood, because there
    # was no handler that stopped the *loop* and dying was the only thing that
    # worked.  A request in flight was dropped, and the master's
    # ``shutdown_timeout`` was a wait rather than a drain.
    #
    # The loop-stopping handler is installed inside ``_serve`` below, where
    # there is a running loop to attach it to.  Until then, SIG_DFL remains
    # the right disposition: a signal arriving before the loop exists has
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

    async def _serve() -> None:
        # Threads inherit the loop thread's affinity mask, so a pinned worker
        # would run every offloaded compression and file read on the one core
        # the loop is already saturating.  Hand the pool the mask the operator
        # gave us instead — offloading exists to get off this core.
        loop = asyncio.get_running_loop()
        if offload_mask is not None:
            loop.set_default_executor(make_offload_executor(offload_mask))

        # SIGTERM now stops the loop instead of the process.  ``stop()`` closes
        # the listeners and waits for the connections already being served;
        # ``add_signal_handler`` is used rather than ``signal.signal`` so the
        # callback runs on the loop, where awaiting is possible.
        #
        # The drain budget sits inside the master's ``shutdown_timeout`` (10 s)
        # so the wait ends in our cancel rather than its SIGKILL.
        def _graceful(*_):
            loop.create_task(server.stop(drain_timeout=_DRAIN_TIMEOUT))

        try:
            loop.add_signal_handler(signal.SIGTERM, _graceful)
        except (NotImplementedError, RuntimeError):
            # No loop signal support (Windows, or a non-main thread).  The
            # SIG_DFL disposition set above still applies.
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
