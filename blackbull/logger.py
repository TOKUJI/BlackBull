import inspect
import logging
import logging.handlers
import queue as _queue_mod
from functools import wraps
from inspect import iscoroutinefunction
from copy import copy
from typing import Literal


def log(fn):
    """Decorator: log call arguments at DEBUG level.

    Automatically uses the logger of the module where ``@log`` is applied,
    determined by inspecting the caller's frame at decoration time.

    When the module logger is not enabled for DEBUG at decoration time (i.e.
    at import), the decorator is a zero-cost no-op: the original function is
    returned unwrapped, so there is no extra function-call overhead in
    production.  The trade-off is that raising the log level to DEBUG after
    modules have been imported will not activate logging for already-decorated
    functions.
    """
    frame = inspect.stack()[1]
    module_name = frame[0].f_globals.get('__name__', 'blackbull')
    _logger = logging.getLogger(module_name)

    if not _logger.isEnabledFor(logging.DEBUG):
        return fn

    if iscoroutinefunction(fn):
        @wraps(fn)
        async def async_wrapper(*args, **kwds):
            _logger.debug('%s(%s, %s)', fn.__name__, args, kwds)
            return await fn(*args, **kwds)
        return async_wrapper
    else:
        @wraps(fn)
        def wrapper(*args, **kwds):
            _logger.debug('%s(%s, %s)', fn.__name__, args, kwds)
            return fn(*args, **kwds)
        return wrapper


# https://stackoverflow.com/questions/384076/how-can-i-color-python-logging-output
MAPPING = {
    'DEBUG'   : 37,  # white
    'INFO'    : 36,  # cyan
    'WARNING' : 33,  # yellow
    'ERROR'   : 31,  # red
    'CRITICAL': 41,  # white on red bg
}

PREFIX = '\033['
SUFFIX = '\033[0m'


class ColoredFormatter(logging.Formatter):

    def __init__(self, fmt=None, datefmt=None,
                 style: Literal['%', '{', '$'] = '%'):
        logging.Formatter.__init__(self, fmt=fmt, datefmt=datefmt, style=style)

    def format(self, record):
        colored_record = copy(record)
        levelname = colored_record.levelname
        seq = MAPPING.get(levelname, 37)  # default white
        colored_levelname = ('{0}{1}m{2}{3}').format(PREFIX, seq, levelname, SUFFIX)

        colored_record.levelname = colored_levelname
        return logging.Formatter.format(self, colored_record)


class _DeferredFormatQueueHandler(logging.handlers.QueueHandler):
    """QueueHandler that defers formatting of self-formatting records.

    The stdlib :class:`~logging.handlers.QueueHandler.prepare` eagerly calls
    ``self.format(record)`` on the *producer* thread (here, the event loop) so
    the record is safe to hand across a process boundary.  For BlackBull's
    access log that means the expensive ``AccessLogRecord.format()`` string
    build runs on the hot path even though the actual write happens on the
    listener thread.

    Records whose message object is marked ``_bb_deferred_format`` (the
    :class:`AccessLogRecord`) are enqueued *without* formatting or copying, so
    the string build moves to the listener thread.  This is safe because the
    queue is in-process (``SimpleQueue`` — no pickling) and an access record is
    created fresh per request and never mutated after emit (its duration is
    snapshotted by ``finalize()``).  Every other record keeps the stdlib's
    eager-format, copy-and-sanitize behaviour, so mutable ``%``-args in
    debug/warning logs still render their value-at-log-time.
    """

    def prepare(self, record: logging.LogRecord) -> logging.LogRecord:
        if getattr(record.msg, '_bb_deferred_format', False):
            return record
        return super().prepare(record)


_listener: logging.handlers.QueueListener | None = None


def setup_async_logging(handlers: list[logging.Handler] | None = None) -> None:
    """Install a QueueHandler on the ``blackbull`` logger hierarchy.

    After this call every ``logger.debug/info/warning`` in the event loop
    enqueues a ``LogRecord`` and returns immediately.  A daemon thread
    (``QueueListener``) drains the queue and forwards records to *handlers*.

    Idempotent — a second call before :func:`teardown_async_logging` is a no-op.

    Parameters
    ----------
    handlers:
        Handlers the background listener should write to.  Defaults to the
        non-NullHandler handlers already on the ``blackbull`` logger, or a
        single ``StreamHandler(stderr)`` when none exist.
    """
    global _listener

    if _listener is not None:
        return

    bb_logger = logging.getLogger('blackbull')

    if handlers is None:
        existing = [h for h in bb_logger.handlers
                    if not isinstance(h, logging.NullHandler)]
        handlers = existing if existing else [logging.StreamHandler()]

    log_queue: _queue_mod.SimpleQueue = _queue_mod.SimpleQueue()
    queue_handler = _DeferredFormatQueueHandler(log_queue)  # type: ignore[arg-type]

    for h in list(bb_logger.handlers):
        bb_logger.removeHandler(h)
    bb_logger.addHandler(queue_handler)

    _listener = logging.handlers.QueueListener(
        log_queue, *handlers, respect_handler_level=True,
    )
    _listener.start()


def teardown_async_logging() -> None:
    """Stop the ``QueueListener`` and restore a ``NullHandler`` on ``blackbull``."""
    global _listener

    if _listener is None:
        return

    _listener.stop()
    _listener = None

    bb_logger = logging.getLogger('blackbull')
    for h in list(bb_logger.handlers):
        if isinstance(h, logging.handlers.QueueHandler):
            bb_logger.removeHandler(h)
    if not bb_logger.handlers:
        bb_logger.addHandler(logging.NullHandler())


if __name__ == '__main__':
    logger = logging.getLogger('blackbull.test')
    logger.setLevel(logging.DEBUG)

    handler = logging.StreamHandler()
    handler.setLevel(logging.DEBUG)
    cf = ColoredFormatter('%(levelname)-17s:%(name)s %(message)s')
    handler.setFormatter(cf)
    logger.addHandler(handler)

    logger.debug(logger.handlers)
    logger.debug('debug')
    logger.info('info')
    logger.warning('warning')
    logger.error('error')
    logger.critical('critical')
