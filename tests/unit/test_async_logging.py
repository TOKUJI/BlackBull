"""Unit tests for blackbull.logger async-logging helpers."""
import json
import logging
import logging.handlers
import time

import pytest

from blackbull.logger import (BatchWriteHandler, JsonFormatter,
                              _build_sink_handlers, setup_async_logging,
                              teardown_async_logging)


@pytest.fixture(autouse=True)
def _cleanup():
    """Ensure async logging is torn down after every test."""
    yield
    teardown_async_logging()


class _CapturingHandler(logging.Handler):
    def __init__(self):
        super().__init__()
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


def test_setup_installs_queue_handler():
    bb = logging.getLogger('blackbull')
    setup_async_logging(handlers=[logging.NullHandler()])
    assert any(isinstance(h, logging.handlers.QueueHandler) for h in bb.handlers)


def test_setup_removes_previous_handlers():
    bb = logging.getLogger('blackbull')
    original_count = len(bb.handlers)
    setup_async_logging(handlers=[logging.NullHandler()])
    # Only the QueueHandler should be present (not extra copies of old handlers).
    assert len(bb.handlers) == 1
    assert isinstance(bb.handlers[0], logging.handlers.QueueHandler)
    _ = original_count  # suppress unused warning


def test_setup_is_idempotent():
    bb = logging.getLogger('blackbull')
    setup_async_logging(handlers=[logging.NullHandler()])
    first_handler = bb.handlers[0]
    setup_async_logging(handlers=[logging.NullHandler()])
    assert len(bb.handlers) == 1
    assert bb.handlers[0] is first_handler


def test_teardown_restores_null_handler():
    setup_async_logging(handlers=[logging.NullHandler()])
    teardown_async_logging()
    bb = logging.getLogger('blackbull')
    assert not any(isinstance(h, logging.handlers.QueueHandler) for h in bb.handlers)
    assert any(isinstance(h, logging.NullHandler) for h in bb.handlers)


def test_teardown_is_idempotent():
    setup_async_logging(handlers=[logging.NullHandler()])
    teardown_async_logging()
    teardown_async_logging()  # second call must not raise


def test_records_reach_handler():
    cap = _CapturingHandler()
    cap.setLevel(logging.DEBUG)
    setup_async_logging(handlers=[cap])

    child = logging.getLogger('blackbull.test_async_records')
    child.setLevel(logging.DEBUG)
    child.info('hello from async logging test')

    # QueueListener runs in a background thread; give it a moment to drain.
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        if cap.records:
            break
        time.sleep(0.01)

    assert cap.records, 'No records delivered to handler'
    assert any('hello from async logging test' in r.getMessage() for r in cap.records)


def test_child_logger_record_reaches_handler():
    cap = _CapturingHandler()
    cap.setLevel(logging.DEBUG)
    setup_async_logging(handlers=[cap])

    # Deeper child — propagation must walk up to the QueueHandler on 'blackbull'.
    logging.getLogger('blackbull.server.connection_actor').warning('actor warning')

    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        if cap.records:
            break
        time.sleep(0.01)

    assert any('actor warning' in r.getMessage() for r in cap.records)


# ---------------------------------------------------------------------------
# JsonFormatter — approach 3 (structured JSON)
# ---------------------------------------------------------------------------

def _make_record(name='blackbull.test', level=logging.INFO, msg='hi',
                 exc_info=None, **extra):
    record = logging.LogRecord(name, level, __file__, 1, msg, (), exc_info)
    for k, v in extra.items():
        setattr(record, k, v)
    return record


def test_json_formatter_emits_base_keys():
    line = JsonFormatter().format(_make_record(msg='hello'))
    obj = json.loads(line)  # must be valid JSON
    assert obj['level'] == 'INFO'
    assert obj['logger'] == 'blackbull.test'
    assert obj['message'] == 'hello'
    assert 'timestamp' in obj


def test_json_formatter_lifts_access_fields():
    """Structured access-log fields (as_extra) become top-level JSON keys."""
    record = _make_record(
        name='blackbull.access', msg='127.0.0.1 "GET / HTTP/1.1" 200 2 1ms',
        client_ip='127.0.0.1', method='GET', path='/', http_version='1.1',
        status=200, response_bytes=2, duration_ms=1.5)
    obj = json.loads(JsonFormatter().format(record))
    assert obj['client_ip'] == '127.0.0.1'
    assert obj['method'] == 'GET'
    assert obj['status'] == 200
    assert obj['response_bytes'] == 2
    assert obj['duration_ms'] == 1.5
    # A field the record does not carry is simply absent (not null).
    assert 'close_code' not in obj


def test_json_formatter_includes_exc_info():
    try:
        raise ValueError('boom')
    except ValueError:
        import sys
        record = _make_record(level=logging.ERROR, msg='failed',
                              exc_info=sys.exc_info())
    obj = json.loads(JsonFormatter().format(record))
    assert 'ValueError: boom' in obj['exc_info']


# ---------------------------------------------------------------------------
# _build_sink_handlers — env-selected destination + format (approaches 3 & 6)
# ---------------------------------------------------------------------------

def test_sink_default_is_plain_stream(monkeypatch):
    monkeypatch.delenv('BB_LOG_FORMAT', raising=False)
    monkeypatch.delenv('BB_SYSLOG_ADDR', raising=False)
    handlers = _build_sink_handlers()
    assert len(handlers) == 1
    assert isinstance(handlers[0], logging.StreamHandler)
    assert not isinstance(handlers[0].formatter, JsonFormatter)


def test_sink_json_format_env(monkeypatch):
    monkeypatch.setenv('BB_LOG_FORMAT', 'json')
    monkeypatch.delenv('BB_SYSLOG_ADDR', raising=False)
    handlers = _build_sink_handlers()
    assert isinstance(handlers[0], logging.StreamHandler)
    assert isinstance(handlers[0].formatter, JsonFormatter)


def test_sink_syslog_addr_env(monkeypatch):
    monkeypatch.setenv('BB_SYSLOG_ADDR', '127.0.0.1:5514')
    monkeypatch.delenv('BB_LOG_FORMAT', raising=False)
    handlers = _build_sink_handlers()
    assert isinstance(handlers[0], logging.handlers.SysLogHandler)
    handlers[0].close()


def test_sink_bad_syslog_addr_falls_back_to_stream(monkeypatch):
    monkeypatch.setenv('BB_SYSLOG_ADDR', 'not-a-port:abc')
    handlers = _build_sink_handlers()
    assert isinstance(handlers[0], logging.StreamHandler)


def test_sink_json_over_syslog(monkeypatch):
    """Approach 3 + 6 compose: JSON lines shipped via syslog."""
    monkeypatch.setenv('BB_SYSLOG_ADDR', '127.0.0.1:5514')
    monkeypatch.setenv('BB_LOG_FORMAT', 'json')
    handlers = _build_sink_handlers()
    assert isinstance(handlers[0], logging.handlers.SysLogHandler)
    assert isinstance(handlers[0].formatter, JsonFormatter)
    handlers[0].close()


# ---------------------------------------------------------------------------
# BatchWriteHandler — approach 4 / O2 (batch writes)
# ---------------------------------------------------------------------------

class _RecordingStream:
    """Stream double recording each write() call as one entry — so tests can
    assert how many write syscalls a batch would produce (the whole point)."""

    def __init__(self):
        self.writes: list[str] = []

    def write(self, data: str) -> None:
        self.writes.append(data)

    def flush(self) -> None:
        pass


def _wait_until(predicate, timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return False


def _plain(handler):
    handler.setFormatter(logging.Formatter('%(message)s'))
    return handler


def test_batch_handler_coalesces_full_batch_into_one_write():
    stream = _RecordingStream()
    h = _plain(BatchWriteHandler(stream, batch_size=3, flush_interval=10.0))
    try:
        for i in range(3):
            h.emit(_make_record(msg=f'line{i}'))
        assert _wait_until(lambda: stream.writes), 'batch never flushed'
        # One write() for the whole batch, lines preserved in order.
        assert stream.writes == ['line0\nline1\nline2\n']
    finally:
        h.close()


def test_batch_handler_flushes_partial_batch_after_interval():
    stream = _RecordingStream()
    h = _plain(BatchWriteHandler(stream, batch_size=100, flush_interval=0.01))
    try:
        h.emit(_make_record(msg='solo'))
        assert _wait_until(lambda: stream.writes), 'interval flush never fired'
        assert stream.writes == ['solo\n']
    finally:
        h.close()


def test_batch_handler_close_flushes_trailing_batch():
    stream = _RecordingStream()
    h = _plain(BatchWriteHandler(stream, batch_size=100, flush_interval=10.0))
    h.emit(_make_record(msg='a'))
    h.emit(_make_record(msg='b'))
    h.close()  # must drain the buffered pair even though neither trigger fired
    assert stream.writes == ['a\nb\n']
    assert not h._flusher.is_alive()  # thread joined


def test_sink_batch_size_env_selects_batch_handler(monkeypatch):
    monkeypatch.setenv('BB_LOG_BATCH_SIZE', '64')
    monkeypatch.delenv('BB_SYSLOG_ADDR', raising=False)
    handlers = _build_sink_handlers()
    try:
        assert isinstance(handlers[0], BatchWriteHandler)
    finally:
        handlers[0].close()


def test_sink_default_batch_size_is_plain_stream(monkeypatch):
    monkeypatch.delenv('BB_LOG_BATCH_SIZE', raising=False)
    monkeypatch.delenv('BB_SYSLOG_ADDR', raising=False)
    handlers = _build_sink_handlers()
    # Default (unset / 1) → no batching, unchanged behaviour.
    assert isinstance(handlers[0], logging.StreamHandler)
    assert not isinstance(handlers[0], BatchWriteHandler)


def test_sink_syslog_ignores_batch_size(monkeypatch):
    """UDP is one datagram per message — batching does not apply to syslog."""
    monkeypatch.setenv('BB_SYSLOG_ADDR', '127.0.0.1:5514')
    monkeypatch.setenv('BB_LOG_BATCH_SIZE', '64')
    handlers = _build_sink_handlers()
    assert isinstance(handlers[0], logging.handlers.SysLogHandler)
    handlers[0].close()


def test_settings_carry_logging_sink_fields(monkeypatch):
    """The sink knobs are first-class Settings fields (discoverable / typed),
    read from BB_* like every other server knob."""
    from blackbull.env import get_settings, reset_settings_cache
    monkeypatch.setenv('BB_LOG_FORMAT', 'json')
    monkeypatch.setenv('BB_SYSLOG_ADDR', '10.0.0.1:514')
    monkeypatch.setenv('BB_LOG_BATCH_SIZE', '64')
    monkeypatch.setenv('BB_LOG_BATCH_TIMEOUT_MS', '3')
    reset_settings_cache()
    try:
        cfg = get_settings()
        assert cfg.log_format == 'json'
        assert cfg.log_syslog_addr == '10.0.0.1:514'
        assert cfg.log_batch_size == 64
        assert cfg.log_batch_timeout_ms == 3
    finally:
        reset_settings_cache()


def test_explicit_params_take_precedence_over_env(monkeypatch):
    """setup_async_logging passes Settings values in explicitly; those must win
    over the env fallback so the typed config is authoritative."""
    monkeypatch.setenv('BB_LOG_FORMAT', 'json')     # env says json…
    handlers = _build_sink_handlers(log_format='')  # …but the caller says plain
    assert not isinstance(handlers[0].formatter, JsonFormatter)


def test_build_sink_handlers_falls_back_to_env_when_none(monkeypatch):
    """None params fall back to env — keeps logger usable standalone."""
    monkeypatch.setenv('BB_LOG_FORMAT', 'json')
    monkeypatch.delenv('BB_SYSLOG_ADDR', raising=False)
    monkeypatch.delenv('BB_LOG_BATCH_SIZE', raising=False)
    handlers = _build_sink_handlers()  # all None → read env
    assert isinstance(handlers[0].formatter, JsonFormatter)


def test_teardown_drains_batch_sink():
    """A record still buffered in the batch sink at teardown must be flushed
    (teardown closes BatchWriteHandler sinks), not lost with the daemon thread."""
    stream = _RecordingStream()
    sink = _plain(BatchWriteHandler(stream, batch_size=100, flush_interval=10.0))
    setup_async_logging(handlers=[sink])

    child = logging.getLogger('blackbull.test_batch_teardown')
    child.setLevel(logging.INFO)
    child.info('buffered')

    # Let the listener dequeue into the batch sink (still buffered — batch not
    # full, long interval), then teardown must drain it.
    _wait_until(lambda: sink._buf or stream.writes)
    teardown_async_logging()
    assert stream.writes == ['buffered\n']
    assert not sink._flusher.is_alive()


def test_json_formatter_covers_real_access_record_extra():
    """Guard against drift: every key AccessLogRecord.as_extra() emits for a
    completed request must surface in the JSON output."""
    from blackbull.server.access_log import AccessLogRecord
    rec = AccessLogRecord(client_ip='10.0.0.1', method='POST', path='/x',
                          http_version='1.1', status=201, response_bytes=7)
    rec.finalize()
    log_record = _make_record(name='blackbull.access', msg=str(rec),
                              **rec.as_extra())
    obj = json.loads(JsonFormatter().format(log_record))
    for key in rec.as_extra():
        assert key in obj, f'{key} from as_extra() missing in JSON output'
