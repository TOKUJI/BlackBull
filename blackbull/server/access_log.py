"""Per-request access-log helpers shared by the HTTP/1.1 and HTTP/2 paths."""
from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass, field
from typing import ClassVar

from ..asgi import ASGIEvent
# Runtime import, not TYPE_CHECKING: beartype resolves the ``EventAggregator``
# union annotations below as expressions against module globals.  Neither
# module imports anything back from this one, so there is no cycle.
from ..event_aggregator import EventAggregator  # noqa: TC002
from ..logger import enqueue_access_log

_access_logger = logging.getLogger('blackbull.access')

# Diagnostic checkpoints are opt-in; their clock reads add request work.
PHASE_TRACE: bool = os.environ.get('BB_PHASE_TRACE', '0') == '1'


def _escape(value: str) -> str:
    """Escape one request-derived value so it cannot end the line or a field."""
    out = []
    for ch in value:
        if ch in '"\\':
            out.append('\\' + ch)
        elif ch.isprintable() and ch != ' ':
            out.append(ch)
        else:
            out.append(f'\\u{ord(ch):04x}')
    return ''.join(out)


def open_record(conn, aggregator: 'EventAggregator | None',
                loop_start: 'tuple[float, float] | None' = None,
                ) -> "AccessLogRecord | None":
    """Open a request record when a consumer exists; otherwise return None.

    Publish in conn.state, shared by compatibility scopes. loop_start seeds
    HTTP/1.1 keep-alive timing; snapshot wire fields before application rewrites.
    """
    if not request_record_needed(aggregator):
        return None
    record = start_record(conn)
    if PHASE_TRACE and loop_start is not None:
        record.phases['loop_start'] = loop_start
    record.mark('parsed')
    return record


def start_record(conn) -> 'AccessLogRecord':
    """Build and publish a record unconditionally.

    For the two paths [`request_record_needed`][] cannot answer for: a
    WebSocket session's record spans the connection and carries ``close_code``,
    and a pushed response needs one for the sender's inline capture.
    """
    record = AccessLogRecord.from_conn(conn)
    # Written onto ``conn.state`` directly — the same dict the scope exposes
    # as ``scope['state']`` — so recording the access log does not materialize
    # the lazy scope.
    conn.state['access_log'] = record
    return record


def close_record(record: "AccessLogRecord | None") -> None:
    """Mark dispatch done and emit; no-op for None.
    """
    if record is None:
        return
    record.mark('dispatch_done')
    emit_access_log(record)


def close_ws_record(record: 'AccessLogRecord | None', close_code) -> None:
    """Emit session close code without a dispatch_done phase; no-op for None.
    """
    if record is None:
        return
    record.close_code = close_code
    emit_access_log(record)


def emit_access_log(record: 'AccessLogRecord') -> None:
    """Emit on the access logger when INFO is enabled.

    Snapshot duration before deferred formatting so queue latency is excluded.
    Keep structured extra fields eager. User handlers or filters require the
    standard logging path; direct enqueue must not bypass them.
    """
    if _access_logger.isEnabledFor(logging.INFO):
        record.finalize()
        extra = record.as_extra()
        if (_access_logger.handlers or _access_logger.filters
                or not enqueue_access_log(record, extra)):
            _access_logger.info(record, extra=extra)


def request_record_needed(aggregator: EventAggregator | None) -> bool:
    """Whether the per-request [`AccessLogRecord`][] will be consumed.

    Three consumers, and no others: the access log (``blackbull.access`` at
    INFO), phase tracing, and the ``request_completed`` event's wire fields.
    With none of them active the actor skips building the record at all, so
    every consumer must tolerate its absence — ``conn.state['access_log']``
    reads back as ``None``, and ``request_completed`` substitutes ``'-'``/``0``
    placeholders."""
    if PHASE_TRACE or _access_logger.isEnabledFor(logging.INFO):
        return True
    return aggregator is not None and aggregator.has_request_completed_listeners()


def disconnect_events_observed(aggregator: EventAggregator | None) -> bool:
    """Whether the disconnect-detecting receive wrapper is observed.

    The wrapper emits ``request_disconnected`` and marks the request so
    ``request_completed`` can suppress itself on a dropped one.  With neither
    listener present nothing observes either effect and the actor dispatches
    the raw ``receive`` instead.  Body-level disconnect detection
    (``conn.body()`` → ``ClientDisconnected``) does not go through it."""
    if aggregator is None:
        return False
    return (aggregator.has_request_disconnected_listeners()
            or aggregator.has_request_completed_listeners())


@dataclass
class AccessLogRecord:
    """Per-request record populated in two phases.

    Phase 1 (after parse): client_ip, method, path, http_version.
    Phase 2 (during send): status, response_bytes.
    For WebSocket sessions, close_code is captured on disconnect instead.
    Emitted as one INFO line on 'blackbull.access' after the response completes.
    """
    client_ip:      str
    method:         str
    path:           str
    http_version:   str
    status:         int | str = '-'
    response_bytes: int       = 0
    close_code:     int | None = None
    # Headers correlated against the per-phase timing.  Populated only under
    # ``PHASE_TRACE``, so production pays no capture; empty bytes read as
    # "header absent" in ``format()``.
    req_accept_encoding:   bytes = b''
    req_range:             bytes = b''
    resp_content_type:     bytes = b''
    resp_content_encoding: bytes = b''
    _started_at:    float     = field(default_factory=time.monotonic, repr=False)
    # name → (perf_counter_seconds, process_time_seconds).  Only written
    # when PHASE_TRACE is on; empty otherwise.
    phases: dict[str, tuple[float, float]] = field(default_factory=dict, repr=False)
    # None until [`finalize`][].
    _duration_ms_snapshot: float | None = field(default=None, repr=False)
    # Filled on first ``str()``, on the listener thread.
    _formatted: str | None = field(default=None, repr=False)

    # Marker read by the deferred-format QueueHandler (blackbull.logger) to
    # move this record's format() off the event-loop thread.  A ClassVar, not
    # a dataclass field, so it is not part of __init__/eq/repr.
    _bb_deferred_format: ClassVar[bool] = True

    def mark(self, name: str) -> None:
        """Capture wall + CPU clocks for *name*.  No-op when phase
        tracing is disabled, so callers don't need to guard themselves."""
        if PHASE_TRACE:
            self.phases[name] = (time.perf_counter(), time.process_time())

    def phase_summary(self) -> str:
        """Format the phase deltas as ``a→b=Wus|Cus a→b=...``."""
        if not self.phases:
            return ''
        items = list(self.phases.items())
        parts = []
        for i in range(1, len(items)):
            (an, (ap, ac)) = items[i - 1]
            (bn, (bp, bc)) = items[i]
            wall_us = int((bp - ap) * 1_000_000)
            cpu_us = int((bc - ac) * 1_000_000)
            parts.append(f'{an}→{bn}={wall_us}w/{cpu_us}c')
        return ' '.join(parts)

    @classmethod
    def from_conn(cls, conn) -> 'AccessLogRecord':
        """Snapshot parsed Connection fields without creating an ASGI scope.
        """
        client = conn.client or ('-',)
        ae = b''
        rng = b''
        if PHASE_TRACE:
            for k, v in conn.headers:
                if isinstance(k, bytes):
                    kl = k.lower()
                    if kl == b'accept-encoding':
                        ae = v
                    elif kl == b'range':
                        rng = v
        return cls(
            client_ip            = str(client[0]),
            method               = conn.method,
            path                 = conn.path,
            http_version         = conn.http_version,
            req_accept_encoding  = ae,
            req_range            = rng,
        )

    def duration_ms(self) -> float:
        # The snapshot keeps the value stable across the emit → enqueue →
        # listener-format hop; a record nothing finalized reads live.
        if self._duration_ms_snapshot is not None:
            return self._duration_ms_snapshot
        return (time.monotonic() - self._started_at) * 1000

    def finalize(self) -> 'AccessLogRecord':
        """Snapshot the duration at completion so a later (deferred) format()
        reports the request's real duration rather than duration + the time the
        record waited in the logging queue.  Idempotent; returns ``self``."""
        if self._duration_ms_snapshot is None:
            self._duration_ms_snapshot = (time.monotonic() - self._started_at) * 1000
        return self

    def __str__(self) -> str:
        """Format once and cache for multiple logging sinks.
        """
        if self._formatted is None:
            self._formatted = self.format()
        return self._formatted

    def format(self) -> str:
        if self.close_code is not None:
            return (f'{_escape(self.client_ip)} '
                    f'"{_escape(self.method)} {_escape(self.path)} '
                    f'WS/{_escape(self.http_version)}" '
                    f'101 close={self.close_code} '
                    f'{self.duration_ms():.0f}ms')
        # Phase tracing needs sub-millisecond resolution and header-level
        # visibility into negotiation, so it bumps %.0f to %.3f and appends
        # the deltas and the captured headers.
        if PHASE_TRACE and self.phases:
            def _h(b: bytes) -> str:
                return _escape(b.decode('ascii', errors='replace')) if b else '-'
            return (f'{_escape(self.client_ip)} '
                    f'"{_escape(self.method)} {_escape(self.path)} '
                    f'HTTP/{_escape(self.http_version)}" '
                    f'{self.status} {self.response_bytes} '
                    f'{self.duration_ms():.3f}ms  '
                    f'req[ae={_h(self.req_accept_encoding)} '
                    f'range={_h(self.req_range)}] '
                    f'resp[ct={_h(self.resp_content_type)} '
                    f'ce={_h(self.resp_content_encoding)}] '
                    f'[{self.phase_summary()}]')
        return (f'{_escape(self.client_ip)} '
                f'"{_escape(self.method)} {_escape(self.path)} '
                f'HTTP/{_escape(self.http_version)}" '
                f'{self.status} {self.response_bytes} '
                f'{self.duration_ms():.0f}ms')

    def as_extra(self) -> dict:
        d: dict = {
            'client_ip':      self.client_ip,
            'method':         self.method,
            'path':           self.path,
            'http_version':   self.http_version,
            'status':         self.status,
            'response_bytes': self.response_bytes,
            'duration_ms':    self.duration_ms(),
        }
        if self.close_code is not None:
            d['close_code'] = self.close_code
        return d



def _make_disconnect_detecting_receive(receive, conn, aggregator: EventAggregator):
    """Wrap *receive* to emit request_disconnected when http.disconnect is seen.

    Used by both the HTTP/1.1 and HTTP/2 actor paths.
    Marks *conn* disconnected on first detection (idempotent).
    """
    from ..connection import disconnected, mark_disconnected  # noqa: PLC0415
    async def detecting_receive():
        event = await receive()
        if isinstance(event, dict) and event.get('type') == ASGIEvent.HTTP_DISCONNECT:
            if not disconnected(conn):
                mark_disconnected(conn)
                await aggregator.on_request_disconnected(conn)
        return event
    return detecting_receive
