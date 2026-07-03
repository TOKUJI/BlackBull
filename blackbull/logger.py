import inspect
import json
import logging
import logging.handlers
import os
import queue as _queue_mod
import sys
import threading
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


class JsonFormatter(logging.Formatter):
    """Emit one JSON object per log line (logging approach 3 — structured JSON).

    Every record carries ``timestamp`` / ``level`` / ``logger`` / ``message``.
    Access-log records (``blackbull.access``) additionally attach the structured
    fields from :meth:`AccessLogRecord.as_extra` via ``extra=`` — those are
    lifted to first-class JSON keys (``client_ip``, ``method``, ``path``,
    ``http_version``, ``status``, ``response_bytes``, ``duration_ms``, and
    ``close_code`` for WebSocket disconnects).  ``exc_info`` is rendered as a
    formatted traceback string when present.

    Runs on the ``QueueListener`` thread (it is the sink handler's formatter),
    so the access record's ``format()`` string build still happens off the
    event loop, exactly as with the plain-text default.
    """

    # Keys AccessLogRecord.as_extra() may attach to the LogRecord.  Emitted as
    # top-level JSON keys when present; absent for normal framework logs.
    _ACCESS_KEYS = ('client_ip', 'method', 'path', 'http_version',
                    'status', 'response_bytes', 'duration_ms', 'close_code')

    def format(self, record: logging.LogRecord) -> str:
        payload: dict = {
            'timestamp': self.formatTime(record, self.datefmt),
            'level':     record.levelname,
            'logger':    record.name,
            'message':   record.getMessage(),
        }
        for key in self._ACCESS_KEYS:
            value = getattr(record, key, None)
            if value is not None:
                payload[key] = value
        if record.exc_info:
            payload['exc_info'] = self.formatException(record.exc_info)
        # default=str so a stray non-serialisable ``extra`` never crashes the
        # logging thread — it degrades to that value's repr instead.
        return json.dumps(payload, default=str)


class BatchWriteHandler(logging.Handler):
    """Coalesce formatted records into one write per batch (O2 — batch writes,
    a.k.a. logging approach 4).

    The stdlib ``StreamHandler`` issues ``stream.write()`` + ``stream.flush()``
    per record — one flushed write syscall per log line.  Under a high-rate
    access log that is the dominant cost on the listener thread.  This handler
    instead appends each formatted line to an in-memory buffer and lets a single
    long-lived flusher thread emit the batch as one ``write()`` when the buffer
    reaches ``batch_size`` **or** ``flush_interval`` seconds elapse — whichever
    comes first, so latency is bounded at low rate and syscalls collapse at high
    rate.

    Design notes:
    - **One** flusher thread total (not a ``threading.Timer`` per batch — that
      would churn a thread per ~``batch_size`` records under load).  It waits on
      a ``Condition`` with the flush interval as its timeout: a full batch
      notifies it awake, an idle interval wakes it to drain a partial batch.
    - Formatting runs on the flusher thread, so a deferred-format access record
      still builds its string off the event loop (as with the plain default).
    - ``close()`` drains the buffer and joins the thread, so a partial trailing
      batch is never lost at teardown.

    Opt-in: only constructed when ``BB_LOG_BATCH_SIZE`` > 1.
    """

    def __init__(self, stream=None, *, batch_size: int = 128,
                 flush_interval: float = 0.005):
        super().__init__()
        self._stream = stream if stream is not None else sys.stderr
        self._batch_size = max(2, batch_size)
        self._interval = flush_interval
        self._buf: list[str] = []
        self._cv = threading.Condition()
        self._closed = False
        self._flusher = threading.Thread(
            target=self._run, name='bb-log-batch', daemon=True)
        self._flusher.start()

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = self.format(record)
        except Exception:  # noqa: BLE001 — mirror logging.Handler.emit contract
            self.handleError(record)
            return
        with self._cv:
            was_empty = not self._buf
            self._buf.append(msg)
            # Wake the flusher when a batch opens (start the interval clock) or
            # fills (flush now).  In between, it sleeps — no per-record wakeup.
            if was_empty or len(self._buf) >= self._batch_size:
                self._cv.notify()

    def _run(self) -> None:
        while True:
            with self._cv:
                while not self._buf and not self._closed:
                    self._cv.wait()  # idle: block until the first record or close
                if self._closed and not self._buf:
                    return
                # A batch is open: give it up to `interval` to fill (a full-batch
                # emit notifies us awake early), then drain whatever accumulated.
                if len(self._buf) < self._batch_size and not self._closed:
                    self._cv.wait(self._interval)
                batch = self._buf
                self._buf = []
                closing = self._closed
            if batch:
                self._write_batch(batch)
            if closing:
                return

    def _write_batch(self, batch: list[str]) -> None:
        try:
            self._stream.write('\n'.join(batch) + '\n')
            self._stream.flush()
        except Exception:  # noqa: BLE001
            # A broken sink must not kill the flusher thread; report once per
            # batch via the stdlib handler-error path (honours logging.raiseExceptions).
            self.handleError(logging.makeLogRecord({'msg': 'batch write failed'}))

    def close(self) -> None:
        with self._cv:
            if self._closed:
                return
            self._closed = True
            self._cv.notify()
        self._flusher.join(timeout=1.0)
        super().close()


def _build_sink_handlers(
    *,
    log_format: str | None = None,
    syslog_addr: str | None = None,
    batch_size: int | None = None,
    batch_timeout_ms: int | None = None,
) -> list[logging.Handler]:
    """Build the async-logging sink handler(s).

    Selects the *destination* and *format* for the background listener when
    :func:`setup_async_logging` is called without explicit handlers (the
    common path — the app has only the ``NullHandler`` from import):

    - *syslog_addr* ``host:port`` → a UDP ``SysLogHandler`` (approach 6).
      An unparseable value falls back to ``stderr`` with a warning rather than
      crashing startup.
    - *batch_size* > 1 → a batching ``stderr`` sink (approach 4 / O2) that
      coalesces up to that many lines into one write, flushed after
      *batch_timeout_ms* (default 5 ms).  Ignored for the syslog sink (UDP is
      one datagram per message).
    - otherwise → ``StreamHandler(stderr)`` (approaches 1/2, the default).

    Formatter: ``JsonFormatter`` when *log_format* is ``json`` (approach 3),
    otherwise the stdlib default (plain text — unchanged behaviour).

    Each parameter defaults to ``None`` → the corresponding ``BB_*`` env var.
    The typed server config (``Settings``/CLI) passes resolved values in via
    :func:`setup_async_logging`; the env fallback keeps ``logger`` usable
    standalone (tests, direct use) without importing the settings stack.
    """
    if log_format is None:
        log_format = os.environ.get('BB_LOG_FORMAT', '')
    if syslog_addr is None:
        syslog_addr = os.environ.get('BB_SYSLOG_ADDR', '')
    if batch_size is None:
        batch_size = _int_env('BB_LOG_BATCH_SIZE', 1)
    if batch_timeout_ms is None:
        batch_timeout_ms = _int_env('BB_LOG_BATCH_TIMEOUT_MS', 5)

    handler: logging.Handler
    syslog_addr = syslog_addr.strip()
    if syslog_addr:
        try:
            host, _, port = syslog_addr.partition(':')
            handler = logging.handlers.SysLogHandler(
                address=(host or '127.0.0.1', int(port) if port else 514))
        except (ValueError, OSError) as exc:
            logging.getLogger('blackbull').warning(
                'BB_SYSLOG_ADDR=%r unusable (%s); falling back to stderr',
                syslog_addr, exc)
            handler = logging.StreamHandler()
    elif batch_size > 1:
        handler = BatchWriteHandler(sys.stderr, batch_size=batch_size,
                                    flush_interval=max(0, batch_timeout_ms) / 1000.0)
    else:
        handler = logging.StreamHandler()

    if log_format.strip().lower() == 'json':
        handler.setFormatter(JsonFormatter())
    return [handler]


def _int_env(name: str, default: int) -> int:
    """Read an int env var, falling back to *default* on unset/unparseable.

    Local to ``logger`` (mirrors ``blackbull.env._int_env``) so the module
    stays free of the settings stack — logging is configured before, and
    independently of, ``get_settings()``.
    """
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


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


def setup_async_logging(
    handlers: list[logging.Handler] | None = None,
    *,
    log_format: str | None = None,
    syslog_addr: str | None = None,
    batch_size: int | None = None,
    batch_timeout_ms: int | None = None,
) -> None:
    """Install a QueueHandler on the ``blackbull`` logger hierarchy.

    After this call every ``logger.debug/info/warning`` in the event loop
    enqueues a ``LogRecord`` and returns immediately.  A daemon thread
    (``QueueListener``) drains the queue and forwards records to *handlers*.

    Idempotent — a second call before :func:`teardown_async_logging` is a no-op.

    Parameters
    ----------
    handlers:
        Handlers the background listener should write to.  Defaults to the
        non-NullHandler handlers already on the ``blackbull`` logger, or the
        sink built by :func:`_build_sink_handlers` when none exist.
    log_format, syslog_addr, batch_size, batch_timeout_ms:
        Sink configuration forwarded to :func:`_build_sink_handlers` (only used
        when *handlers* is None and no handlers are pre-attached).  The server
        startup passes these from ``Settings`` (``get_settings()``); each
        defaults to ``None`` → the matching ``BB_*`` env var.
    """
    global _listener

    if _listener is not None:
        return

    bb_logger = logging.getLogger('blackbull')

    if handlers is None:
        existing = [h for h in bb_logger.handlers
                    if not isinstance(h, logging.NullHandler)]
        handlers = existing if existing else _build_sink_handlers(
            log_format=log_format, syslog_addr=syslog_addr,
            batch_size=batch_size, batch_timeout_ms=batch_timeout_ms)

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
    # Drain and stop any batching sink so a partial trailing batch is flushed
    # (its flusher is a daemon thread that would otherwise be killed at exit).
    for h in _listener.handlers:
        if isinstance(h, BatchWriteHandler):
            h.close()
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
