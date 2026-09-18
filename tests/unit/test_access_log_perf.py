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
from blackbull.server import access_log
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


# ---------------------------------------------------------------------------
# A request-derived value is data, not structure
# ---------------------------------------------------------------------------

#: Structure-bearing characters, each between ``a`` and ``b``.
_HOSTILE = ['/a\nb', '/a\rb', '/a\r\nb', '/a\tb', '/a\x1bb', '/a\x85b',
            '/a\u2028b', '/a\u2029b', '/a"b', '/a\\b', '/a b']

_FIELDS = ['client_ip', 'method', 'path', 'http_version']


@pytest.mark.parametrize('field', _FIELDS)
@pytest.mark.parametrize('payload', _HOSTILE)
def test_a_request_value_cannot_leave_the_line(field, payload):
    line = _record(**{field: payload}).format()

    assert line.splitlines() == [line]
    assert line.isprintable()
    # Escaped rather than dropped: the value is still readable on the line.
    assert payload[0] in line and payload[-1] in line


@pytest.mark.parametrize('field', _FIELDS)
@pytest.mark.parametrize('payload', _HOSTILE)
def test_the_structured_value_stays_raw(field, payload):
    """``extra``/JSON carries the value itself, so a consumer must not have to
    un-escape what the text line had to escape."""
    assert _record(**{field: payload}).as_extra()[field] == payload


@pytest.mark.parametrize('path', ['/review', '/\u65e5\u672c\u8a9e/\u30da\u30fc\u30b8',
                                  '/a%20b', "/o'brien", '/x;y=1,2'])
def test_a_legal_path_is_logged_as_it_arrived(path):
    line = _record(path=path).format()
    assert path in line
    assert line.count('"') == 2      # only the two field boundaries


def test_a_decoded_space_is_a_path_character_not_a_field_boundary():
    """``/a%20b`` is a legal URL and arrives as ``/a b``: the request line must
    still split into exactly the three fields it has."""
    line = _record(method='GET', path='/a b', http_version='1.1').format()

    assert line.split('"')[1].split() == ['GET', '/a\\u0020b', 'HTTP/1.1']


@pytest.mark.parametrize('payload', _HOSTILE)
def test_the_websocket_line_escapes_its_request_line(payload):
    line = _record(path=payload, close_code=1000).format()
    assert line.splitlines() == [line]
    assert line.isprintable()


@pytest.mark.parametrize('payload', [b'0-1"x', b'a\\b', b'a\nb', b'\xff'])
def test_the_phase_trace_line_escapes_captured_headers(monkeypatch, payload):
    monkeypatch.setattr(access_log, 'PHASE_TRACE', True)
    rec = _record(path='/x', phases={'a': (0.0, 0.0), 'b': (0.001, 0.0005)},
                  req_accept_encoding=payload, req_range=payload,
                  resp_content_type=payload, resp_content_encoding=payload)
    line = rec.format()
    assert line.splitlines() == [line]
    assert line.isprintable()


def test_a_hostile_path_reaches_the_sink_as_one_line(_cleanup):
    cap = _Capture()
    cap.setLevel(logging.INFO)
    setup_async_logging(handlers=[cap])
    logging.getLogger('blackbull.access').setLevel(logging.INFO)

    emit_access_log(_record(path='/a\nb forged', status=200))

    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline and not cap.records:
        time.sleep(0.01)

    assert cap.records, 'access record never reached the listener'
    lr = cap.records[-1]
    assert len(lr.getMessage().splitlines()) == 1
    # ...and the structured field keeps the value the request had.
    assert lr.path == '/a\nb forged'


class _Conn:
    """The little of ``Connection`` that ``start_record`` reads."""

    client = ('127.0.0.1', 4711)
    method = 'GET'
    path = '/a\nb'
    http_version = '1.1'
    headers: list = []
    state: dict = {}


def test_a_disabled_logger_does_no_escaping_work(monkeypatch):
    """Escaping lives in the text line, so a box that turned the access log off
    pays nothing for it: the record is not formatted, and nothing is escaped —
    not even by the builder a WebSocket or pushed response goes through."""
    escaped: list[str] = []
    monkeypatch.setattr(access_log, '_escape', lambda v: escaped.append(v) or v)

    acc = logging.getLogger('blackbull.access')
    previous = acc.level
    acc.setLevel(logging.WARNING)
    try:
        rec = access_log.start_record(_Conn())
        emit_access_log(rec)
        assert rec._formatted is None
        assert escaped == []
    finally:
        acc.setLevel(previous)

    # Positive control: formatting is where the escaping happens.
    _record(path='/a\nb').format()
    assert escaped
