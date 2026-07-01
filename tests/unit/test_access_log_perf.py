"""Unit tests for the deferred-format access-log fast path.

Covers the O1 hot-path optimization: the access record is self-formatting and
handed to ``logger.info`` as the message so its ``format()`` string build runs
on the logging *listener* thread (via ``_DeferredFormatQueueHandler``) rather
than the event loop, while structured ``extra`` fields stay eager and the
logged duration is snapshotted at emit.
"""
from __future__ import annotations

import logging
import logging.handlers
import queue
import time

import pytest

from blackbull.logger import (
    _DeferredFormatQueueHandler, setup_async_logging, teardown_async_logging,
)
from blackbull.server.access_log import AccessLogRecord, emit_access_log


def _record(**kw) -> AccessLogRecord:
    base = dict(client_ip='127.0.0.1', method='GET', path='/x',
                http_version='1.1', status=200, response_bytes=3)
    base.update(kw)
    return AccessLogRecord(**base)


# ---------------------------------------------------------------------------
# finalize() / duration snapshot
# ---------------------------------------------------------------------------

def test_finalize_snapshots_duration_stable_across_delay():
    rec = _record()
    rec.finalize()
    snap = rec.duration_ms()
    time.sleep(0.02)
    # Deferred format on the listener thread happens *after* the record waited
    # in the queue; duration must not grow to include that wait.
    assert rec.duration_ms() == snap


def test_finalize_is_idempotent():
    rec = _record()
    rec.finalize()
    first = rec.duration_ms()
    rec.finalize()
    assert rec.duration_ms() == first


def test_duration_live_without_finalize():
    rec = _record()
    d1 = rec.duration_ms()
    time.sleep(0.02)
    # No finalize() → live reading, so it advances.
    assert rec.duration_ms() > d1


# ---------------------------------------------------------------------------
# self-formatting message
# ---------------------------------------------------------------------------

def test_str_equals_format_and_is_cached():
    rec = _record()
    s1 = str(rec)
    assert s1 == rec.format()
    # Cached: second str() returns the same object (no re-build).
    assert str(rec) is s1


def test_str_reflects_snapshot_duration():
    rec = _record()
    rec.finalize()
    time.sleep(0.02)
    # The formatted line must use the snapshot, not a fresh (larger) reading.
    assert f'{rec.duration_ms():.0f}ms' in str(rec)


# ---------------------------------------------------------------------------
# _DeferredFormatQueueHandler.prepare
# ---------------------------------------------------------------------------

def _logrecord(msg) -> logging.LogRecord:
    return logging.LogRecord('blackbull.access', logging.INFO,
                             __file__, 1, msg, (), None)


def test_prepare_defers_access_records():
    handler = _DeferredFormatQueueHandler(queue.SimpleQueue())
    rec = _record()
    lr = _logrecord(rec)
    prepared = handler.prepare(lr)
    # Returned unchanged (no eager format, no copy): message object preserved,
    # so the listener thread does the format().
    assert prepared is lr
    assert prepared.msg is rec


def test_prepare_eager_formats_normal_records():
    handler = _DeferredFormatQueueHandler(queue.SimpleQueue())
    lr = _logrecord('plain %s')
    lr.args = ('value',)
    prepared = handler.prepare(lr)
    # Stdlib behaviour for non-access records: formatted + args cleared.
    assert prepared.message == 'plain value'
    assert prepared.args is None


# ---------------------------------------------------------------------------
# end-to-end through the async listener
# ---------------------------------------------------------------------------

class _Capture(logging.Handler):
    def __init__(self):
        super().__init__()
        self.records: list[logging.LogRecord] = []

    def emit(self, record):
        # Force formatting (what a real sink does) so we exercise the deferred
        # path on the listener thread.
        record.getMessage()
        self.records.append(record)


@pytest.fixture
def _cleanup():
    yield
    teardown_async_logging()


def test_emit_reaches_listener_with_structured_fields(_cleanup):
    cap = _Capture()
    cap.setLevel(logging.INFO)
    setup_async_logging(handlers=[cap])

    acc = logging.getLogger('blackbull.access')
    acc.setLevel(logging.INFO)

    rec = _record(path='/deferred', status=201)
    emit_access_log(rec)

    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline and not cap.records:
        time.sleep(0.01)

    assert cap.records, 'access record never reached the listener'
    lr = cap.records[-1]
    # Deferred format produced the right line on the listener thread...
    assert '/deferred' in lr.getMessage()
    assert '201' in lr.getMessage()
    # ...and the structured extra fields survived (public API contract).
    assert lr.path == '/deferred'
    assert lr.status == 201
    assert lr.client_ip == '127.0.0.1'
    assert isinstance(lr.duration_ms, float)
