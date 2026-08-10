"""Per-request access-log helpers shared by the HTTP/1.1 and HTTP/2 paths."""
from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass, field
from typing import ClassVar

from ..asgi import ASGIEvent
# Imported at runtime (not under TYPE_CHECKING) so beartype can resolve the
# ``EventAggregator`` union annotations below (with ``from __future__ import
# annotations`` beartype parses them as expressions against module globals).
# No circular-import risk — ``event_aggregator`` does not import anything
# back from this module.
from ..event_aggregator import EventAggregator  # noqa: TC002
from ..logger import enqueue_access_log  # O4 fast path (no import cycle: logger imports nothing here)

_access_logger = logging.getLogger('blackbull.access')

# Capture per-request phase wall + CPU
# checkpoints into AccessLogRecord.phases.  Off by default — the
# extra time.perf_counter() + time.process_time() calls would otherwise
# show up in benchmark numbers.  Set ``BB_PHASE_TRACE=1`` to turn on
# (intended for one-off perf investigation runs, not production).
PHASE_TRACE: bool = os.environ.get('BB_PHASE_TRACE', '0') == '1'


def open_record(conn, aggregator: 'EventAggregator | None',
                loop_start: 'tuple[float, float] | None' = None,
                ) -> "AccessLogRecord | None":
    """Start a request's access-log record, or ``None`` if nothing reads it.

    The one place that answers "does this request need a record, and if so
    what does a fresh one look like".  Both protocol actors call it; neither
    decides the gate, builds the record, or knows that ``conn.state`` is where
    it is published.

    *loop_start* seeds the phase trace with the keep-alive loop's entry
    timestamps, which only the H/1 actor has to give.

    The record is always built from the ``Connection``, never from an ASGI
    scope: the actor has the parsed Connection on every lane, and on the
    ``BB_FORCE_ASGI_SCOPE`` lane the emitted scope shares ``conn.state`` by
    identity — so the app's rebuilt Connection reads back the same record.
    That sharing is the contract ``BlackBull._dispatch`` relies on to source
    ``request_completed``'s wire fields.
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

    For the paths whose consumer analysis is not the per-request one:
    a WebSocket session's record spans the connection and carries
    ``close_code``, and a pushed response needs one for the sender's inline
    capture.  Both want a record regardless of what
    :func:`request_record_needed` says about ordinary requests.
    """
    record = AccessLogRecord.from_conn(conn)
    # Written onto ``conn.state`` directly — the same dict the scope exposes
    # as ``scope['state']`` — so recording the access log does not materialize
    # the lazy scope.
    conn.state['access_log'] = record
    return record


def close_record(record: "AccessLogRecord | None") -> None:
    """Finish a request's record and emit it.  A no-op when there is none.

    Paired with :func:`open_record`, so a caller that opened a record does not
    also have to remember the final ``mark`` or repeat the ``is not None``
    guard at every dispatch exit.
    """
    if record is None:
        return
    record.mark('dispatch_done')
    emit_access_log(record)


def close_ws_record(record: 'AccessLogRecord | None', close_code) -> None:
    """Finish a WebSocket session's record and emit it.  A no-op when there is
    none — a session that never opened a record (no consumer, per
    :func:`request_record_needed`) must not crash its close path.

    A session is not a request dispatch: it has no ``dispatch_done`` phase,
    and what it reports instead is the close code the peer or the server
    ended on.  Separate from :func:`close_record` for that reason, so neither
    protocol actor has to know which terminal field belongs to which shape.
    """
    if record is None:
        return
    record.close_code = close_code
    emit_access_log(record)


def emit_access_log(record: 'AccessLogRecord') -> None:
    """Emit *record* on the access logger if INFO is enabled.

    The isEnabledFor gate matters: ``record.as_extra()`` and (formerly)
    ``record.format()`` are evaluated before ``logger.info`` decides to
    discard the call.  Profiling at -R 5000 with BB_ACCESS_LOG=0 showed
    these calls still costing ~1.2% of CPU.  Peers (uvicorn / granian /
    daphne) skip the work entirely when access logging is disabled; gating
    here matches that behaviour.

    The *record itself* is the message (it is self-formatting via ``__str__``),
    so the expensive ``format()`` string build is deferred to the logging
    listener thread instead of running on the event loop.  ``finalize()``
    snapshots the duration first so that deferred format still reports the
    request's real duration, not duration + queue latency.  The structured
    ``extra`` fields stay eager — they are the documented public access-log API
    (guide.md §14; ``tests/integration/test_access_log.py``).

    When async logging is active *and* the access logger has not been customised,
    the record is enqueued directly onto the listener queue via
    :func:`~blackbull.logger.enqueue_access_log` (O4), which bypasses
    ``logging.Logger._log`` — ~93% of the loop-side emit cost lives in that
    stdlib machinery.  The fast path is skipped (and the standard synchronous
    ``logger.info`` path used) when async logging is off *or* the user has
    attached their own handlers/filters to ``blackbull.access`` — those would be
    bypassed by a direct enqueue, so we defer to the full path to keep the
    documented "extend the access log via a custom handler/filter" pattern
    working (see docs/guide/logging.md).
    """
    if _access_logger.isEnabledFor(logging.INFO):
        record.finalize()
        extra = record.as_extra()
        # Fast path only when nobody has customised blackbull.access — an empty
        # handlers/filters list is the default, so the common case stays fast.
        if (_access_logger.handlers or _access_logger.filters
                or not enqueue_access_log(record, extra)):
            _access_logger.info(record, extra=extra)


def request_record_needed(aggregator: EventAggregator | None) -> bool:
    """Whether the per-request :class:`AccessLogRecord` will be consumed.

    The record (and the ``conn.state['access_log']`` write it forces, plus the
    ``emit`` at request end) exists only for three consumers: the access log
    (``blackbull.access`` at INFO), phase tracing, and the ``request_completed``
    event's wire fields. When none is active the record is dead weight on every
    request — a per-request allocation the v0.60.0 Connection graph makes more
    costly under concurrency (extra live objects for the cyclic GC to scan) —
    so the actor skips building it. Consumers already tolerate its absence: the
    sender guards ``if self._log_record is not None`` and ``request_completed``
    reads ``conn.state.get('access_log')`` with ``'-'``/``0`` placeholders."""
    if PHASE_TRACE or _access_logger.isEnabledFor(logging.INFO):
        return True
    return aggregator is not None and aggregator.has_request_completed_listeners()


def disconnect_events_observed(aggregator: EventAggregator | None) -> bool:
    """Whether the disconnect-detecting receive wrapper is observed.

    The wrapper (a per-request closure) exists to (a) emit ``request_disconnected``
    and (b) ``mark_disconnected`` so ``request_completed`` can suppress itself on
    a dropped request. With neither listener present nothing observes either
    effect, so the actor dispatches the raw ``receive`` directly and saves the
    closure. Body-level disconnect detection (``conn.body()`` →
    ``ClientDisconnected``) is independent of this wrapper and unaffected."""
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
    # Request/response headers we want
    # to correlate against per-phase timing.  Empty bytes are interpreted
    # as "header absent" in ``format()``.  Populated only when
    # ``PHASE_TRACE=1`` so production responses don't pay the bytes
    # capture per request.
    req_accept_encoding:   bytes = b''
    req_range:             bytes = b''
    resp_content_type:     bytes = b''
    resp_content_encoding: bytes = b''
    _started_at:    float     = field(default_factory=time.monotonic, repr=False)
    # name → (perf_counter_seconds, process_time_seconds).  Only written
    # when PHASE_TRACE is on; empty otherwise.
    phases: dict[str, tuple[float, float]] = field(default_factory=dict, repr=False)
    # Duration snapshot taken by finalize() at emit time so a format() run
    # later on the logging listener thread reports the real request duration
    # (not duration + queue latency).  None until finalize()/emit.
    _duration_ms_snapshot: float | None = field(default=None, repr=False)
    # Cached format() output — filled on first str() (listener thread).  Cached
    # because several sink handlers may each format the same record.
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
        """Build directly from a :class:`~blackbull.connection.Connection`
        so the self-hosted actor never materializes the ASGI
        scope just to record the access line."""
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
        # Return the finalize() snapshot when present so the value is stable
        # across the emit → enqueue → listener-format hop; fall back to a live
        # reading for records that were never finalized (e.g. direct callers).
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
        """Self-formatting message body.  ``emit_access_log`` hands the record
        to ``logger.info`` as the message so this — and the ``format()`` string
        build it wraps — runs on the logging listener thread, not the event
        loop.  Cached because several sink handlers may format the same record."""
        if self._formatted is None:
            self._formatted = self.format()
        return self._formatted

    def format(self) -> str:
        if self.close_code is not None:
            return (f'{self.client_ip} '
                    f'"{self.method} {self.path} WS/{self.http_version}" '
                    f'101 close={self.close_code} '
                    f'{self.duration_ms():.0f}ms')
        # Default to %.0f ms (existing access-log format).  When phase
        # tracing is on, bump to %.3f and append the per-phase deltas
        # plus request / response headers we want to correlate against
        # per-phase timing — the investigation needs sub-millisecond
        # resolution and header-level visibility into negotiation.
        if PHASE_TRACE and self.phases:
            def _h(b: bytes) -> str:
                return b.decode('ascii', errors='replace') if b else '-'
            return (f'{self.client_ip} '
                    f'"{self.method} {self.path} HTTP/{self.http_version}" '
                    f'{self.status} {self.response_bytes} '
                    f'{self.duration_ms():.3f}ms  '
                    f'req[ae={_h(self.req_accept_encoding)} '
                    f'range={_h(self.req_range)}] '
                    f'resp[ct={_h(self.resp_content_type)} '
                    f'ce={_h(self.resp_content_encoding)}] '
                    f'[{self.phase_summary()}]')
        return (f'{self.client_ip} '
                f'"{self.method} {self.path} HTTP/{self.http_version}" '
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
