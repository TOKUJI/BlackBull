"""Per-request access-log helpers shared by the HTTP/1.1 and HTTP/2 paths."""
from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass, field
from typing import ClassVar

from ..asgi import ASGIEvent
# Imported at runtime (not under TYPE_CHECKING) so beartype can resolve
# the ``'EventAggregator'`` forward reference on
# ``_make_disconnect_detecting_receive``.  No circular-import risk —
# ``event_aggregator`` does not import anything back from this module.
from ..event_aggregator import EventAggregator  # noqa: TC002

_access_logger = logging.getLogger('blackbull.access')

# Sprint 33 investigation knob: capture per-request phase wall + CPU
# checkpoints into AccessLogRecord.phases.  Off by default — the
# extra time.perf_counter() + time.process_time() calls would otherwise
# show up in benchmark numbers.  Set ``BB_PHASE_TRACE=1`` to turn on
# (intended for one-off perf investigation runs, not production).
PHASE_TRACE: bool = os.environ.get('BB_PHASE_TRACE', '0') == '1'


def emit_access_log(record: 'AccessLogRecord') -> None:
    """Emit *record* on the access logger if INFO is enabled.

    The isEnabledFor gate matters: ``record.as_extra()`` and (formerly)
    ``record.format()`` are evaluated before ``logger.info`` decides to
    discard the call.  Profiling at -R 5000 with BB_ACCESS_LOG=0 showed
    these calls still costing ~1.2% of CPU.  Peers (uvicorn / granian /
    daphne) skip the work entirely when access logging is disabled; gating
    here matches that behaviour.

    The *record itself* is handed to ``logger.info`` as the message (it is
    self-formatting via ``__str__``), so the expensive ``format()`` string
    build is deferred to the logging listener thread by the deferred-format
    QueueHandler (``blackbull.logger``) instead of running on the event loop.
    ``finalize()`` snapshots the duration first so that deferred format still
    reports the request's real duration, not duration + queue latency.  The
    structured ``extra`` fields stay eager — they are the documented public
    access-log API (guide.md §14; ``tests/integration/test_access_log.py``).
    """
    if _access_logger.isEnabledFor(logging.INFO):
        record.finalize()
        _access_logger.info(record, extra=record.as_extra())


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
    # Sprint 35 phase-trace diagnostic — request/response headers we want
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
    def from_scope(cls, scope: dict) -> 'AccessLogRecord':
        client = scope.get('client') or ['-']
        ae = b''
        rng = b''
        if PHASE_TRACE:
            for k, v in scope.get('headers', []):
                if isinstance(k, bytes):
                    kl = k.lower()
                    if kl == b'accept-encoding':
                        ae = v
                    elif kl == b'range':
                        rng = v
        return cls(
            client_ip            = str(client[0]),
            method               = scope.get('method', '-'),
            path                 = scope.get('path', '-'),
            http_version         = scope.get('http_version', '-'),
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



def _make_disconnect_detecting_receive(receive, scope: dict, aggregator: 'EventAggregator'):
    """Wrap *receive* to emit request_disconnected when http.disconnect is seen.

    Used by both the HTTP/1.1 and HTTP/2 actor paths.
    Sets scope['_disconnected'] = True on first detection (idempotent).
    """
    async def detecting_receive():
        event = await receive()
        if isinstance(event, dict) and event.get('type') == ASGIEvent.HTTP_DISCONNECT:
            if not scope.get('_disconnected'):
                scope['_disconnected'] = True
                await aggregator.on_request_disconnected(scope)
        return event
    return detecting_receive


def _make_capturing_send(send, record: AccessLogRecord):
    """Wrap *send* to update *record* with status and response size as events flow through.

    When ``BB_PHASE_TRACE=1``, also captures four extra checkpoints around
    the outer send call:
      - ``pre_start_send`` / ``post_start_send`` — bracket the
        ``http.response.start`` write (so ``parsed → pre_start_send``
        attributes to handler + on-entry middleware work, and
        ``pre_start_send → post_start_send`` attributes to the sender's
        header serialise + drain).
      - ``pre_body_send`` / ``post_body_send`` — bracket the last
        ``http.response.body`` write (``more_body=False``).  For chunked
        streaming paths only the final chunk is marked.

    The marks are dict-keyed, so they're free-noop when PHASE_TRACE is off
    (``record.mark()`` returns immediately).
    """
    async def capturing_send(event, *args, **kwargs):
        if isinstance(event, dict):
            ev_type = event.get('type')
            if ev_type == ASGIEvent.HTTP_RESPONSE_START:
                record.status = event.get('status', '-')
                if PHASE_TRACE:
                    for k, v in event.get('headers', []):
                        if isinstance(k, bytes):
                            kl = k.lower()
                            if kl == b'content-type':
                                record.resp_content_type = v
                            elif kl == b'content-encoding':
                                record.resp_content_encoding = v
                record.mark('pre_start_send')
                await send(event, *args, **kwargs)
                record.mark('post_start_send')
                return
            elif ev_type == ASGIEvent.HTTP_RESPONSE_BODY:
                record.response_bytes += len(event.get('body', b''))
                # Only mark the final chunk so the wall delta lines up with
                # ``dispatch_done`` for both the cache-hit single-body and
                # the streaming/pathsend last-chunk path.
                if not event.get('more_body', False):
                    record.mark('pre_body_send')
                    await send(event, *args, **kwargs)
                    record.mark('post_body_send')
                    return
        elif isinstance(event, bytes) and args:
            # _wrap_send calls send(body, status, headers) for simplified handlers
            record.status = int(args[0])
            record.response_bytes += len(event)
        await send(event, *args, **kwargs)
    return capturing_send
