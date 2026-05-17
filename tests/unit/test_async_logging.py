"""Unit tests for blackbull.logger async-logging helpers."""
import logging
import logging.handlers
import time

import pytest

from blackbull.logger import setup_async_logging, teardown_async_logging


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
