"""Queued logging and import-time logging gates.

Configure DEBUG before importing guarded code: raising logger levels later
does not enable log/debug_gate wrappers. Sink I/O belongs off the event loop.
"""
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


def debug_gate(logger: logging.Logger) -> bool:
    """Capture DEBUG enablement at import time.

    Configure the level before importing guarded code; later level changes do not
    update this bool. Guard argument evaluation as well as the logging call.
    """
    return logger.isEnabledFor(logging.DEBUG)


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
    """A ``logging.Formatter`` that colours ``%(levelname)s`` by severity.

    Takes the same ``fmt`` / ``datefmt`` / ``style`` arguments as its base and
    behaves identically except that the level name is wrapped in an ANSI colour
    sequence.  Only the copy of the record being formatted is coloured, so
    other handlers on the same record still see the plain name.

    The escape sequences are written unconditionally — this is a terminal
    format, and pointing it at a file or a pipe puts them in the output.  Use
    [`JsonFormatter`][blackbull.logger.JsonFormatter] or a plain
    ``logging.Formatter`` for anything that is not a terminal.
    """

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
    fields from ``AccessLogRecord.as_extra`` via ``extra=`` — those are
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
    """Flush formatted records when a batch fills or its interval expires.

    One flusher thread handles all batches. close() drains trailing records and
    joins it. Do not inherit this live thread across fork.
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
            # The drained batch is detached from self._buf; it may be mutated.
            batch.append('')
            self._stream.write('\n'.join(batch))
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
    log_file: str | None = None,
) -> list[logging.Handler]:
    """Choose the async sink from explicit values or environment defaults.

    Syslog wins over a file path. Invalid destinations warn and fall back to
    stderr. Open files and start batch threads after fork. Stream/file sinks
    always batch; synchronous logging is the per-record-flush option.
    """
    if log_format is None:
        log_format = os.environ.get('BB_LOG_FORMAT', '')
    if syslog_addr is None:
        syslog_addr = os.environ.get('BB_SYSLOG_ADDR', '')
    if batch_size is None:
        batch_size = _int_env('BB_LOG_BATCH_SIZE', 64)
    if batch_timeout_ms is None:
        batch_timeout_ms = _int_env('BB_LOG_BATCH_TIMEOUT_MS', 5)
    if log_file is None:
        log_file = os.environ.get('BB_LOG_FILE', '')

    # Open each worker's append-mode sink after fork.
    log_file = log_file.strip()
    stream = sys.stderr
    if log_file:
        try:
            stream = open(log_file, 'a', encoding='utf-8')  # noqa: SIM115 — long-lived sink
        except OSError as exc:
            logging.getLogger('blackbull').warning(
                'BB_LOG_FILE=%r unusable (%s); falling back to stderr',
                log_file, exc)
            stream = sys.stderr

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
    else:
        # Batch stream/file writes, including partial batches on timeout.
        handler = BatchWriteHandler(stream, batch_size=batch_size,
                                    flush_interval=max(0, batch_timeout_ms) / 1000.0)

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
    """Defer immutable access-record formatting to the listener thread.

    Only _bb_deferred_format messages may cross this in-process queue unchanged.
    Other records keep stdlib copying and sanitization so mutable arguments
    retain their value at emission. Never mutate a deferred record after emit.
    """

    def prepare(self, record: logging.LogRecord) -> logging.LogRecord:
        if getattr(record.msg, '_bb_deferred_format', False):
            return record
        return super().prepare(record)


_listener: logging.handlers.QueueListener | None = None

# None requires the synchronous fallback. Direct enqueue is only safe
# without custom access-log handlers or filters.
_log_queue: _queue_mod.SimpleQueue | None = None

# A stand-in pathname for access LogRecords built by the fast path.  We do not
# walk the stack (findCaller) for access logs — the source location is fixed and
# uninteresting — so this constant fills the LogRecord's pathname slot.
_ACCESS_PATHNAME = '(blackbull.access)'


def enqueue_access_log(msg: object, extra: dict | None = None) -> bool:
    """Enqueue an access record; return False when synchronous fallback is needed.

    The caller owns the INFO gate. This bypasses handler/filter chains, so use it
    only when the access logger has no user handlers or filters.
    """
    q = _log_queue
    if q is None:
        return False
    record = logging.LogRecord(
        'blackbull.access', logging.INFO, _ACCESS_PATHNAME, 0, msg, None, None)
    if extra:
        record.__dict__.update(extra)
    q.put(record)
    return True


def setup_async_logging(
    handlers: list[logging.Handler] | None = None,
    *,
    log_format: str | None = None,
    syslog_addr: str | None = None,
    batch_size: int | None = None,
    batch_timeout_ms: int | None = None,
    log_file: str | None = None,
) -> None:
    """Install a QueueHandler on the ``blackbull`` logger hierarchy.

    After this call every ``logger.debug/info/warning`` in the event loop
    enqueues a ``LogRecord`` and returns immediately.  A daemon thread
    (``QueueListener``) drains the queue and forwards records to *handlers*.

    Idempotent — a second call before [`teardown_async_logging`][] is a no-op.

    Parameters
    ----------
    handlers:
        Handlers the background listener should write to.  Defaults to the
        non-NullHandler handlers already on the ``blackbull`` logger, or the
        sink built by ``_build_sink_handlers`` when none exist.
    log_format, syslog_addr, batch_size, batch_timeout_ms, log_file:
        Sink configuration forwarded to ``_build_sink_handlers`` (only used
        when *handlers* is None and no handlers are pre-attached).  The server
        startup passes these from ``Settings`` (``get_settings()``); each
        defaults to ``None`` → the matching ``BB_*`` env var.
    """
    global _listener, _log_queue

    if _listener is not None:
        return

    bb_logger = logging.getLogger('blackbull')

    if handlers is None:
        existing = [h for h in bb_logger.handlers
                    if not isinstance(h, logging.NullHandler)]
        handlers = existing if existing else _build_sink_handlers(
            log_format=log_format, syslog_addr=syslog_addr,
            batch_size=batch_size, batch_timeout_ms=batch_timeout_ms,
            log_file=log_file)

    log_queue: _queue_mod.SimpleQueue = _queue_mod.SimpleQueue()
    queue_handler = _DeferredFormatQueueHandler(log_queue)  # type: ignore[arg-type]

    for h in list(bb_logger.handlers):
        bb_logger.removeHandler(h)
    bb_logger.addHandler(queue_handler)

    _listener = logging.handlers.QueueListener(
        log_queue, *handlers, respect_handler_level=True,
    )
    _listener.start()
    _log_queue = log_queue


def teardown_async_logging() -> None:
    """Stop the ``QueueListener`` and restore a ``NullHandler`` on ``blackbull``."""
    global _listener, _log_queue

    if _listener is None:
        return

    _log_queue = None
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
