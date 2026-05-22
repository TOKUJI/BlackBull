"""Per-request access-log helpers shared by the HTTP/1.1 and HTTP/2 paths."""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

from .constants import ASGIEvent
# Imported at runtime (not under TYPE_CHECKING) so beartype can resolve
# the ``'EventAggregator'`` forward reference on
# ``_make_disconnect_detecting_receive``.  No circular-import risk —
# ``event_aggregator`` does not import anything back from this module.
from ..event_aggregator import EventAggregator  # noqa: TC002

_access_logger = logging.getLogger('blackbull.access')


def emit_access_log(record: 'AccessLogRecord') -> None:
    """Emit *record* on the access logger if INFO is enabled.

    The isEnabledFor gate matters: ``record.format()`` and
    ``record.as_extra()`` are evaluated before ``logger.info`` decides to
    discard the call.  Profiling at -R 5000 with BB_ACCESS_LOG=0 showed
    these two calls still costing ~1.2% of CPU.  Peers (uvicorn / granian /
    daphne) skip the work entirely when access logging is disabled; gating
    here matches that behaviour.
    """
    if _access_logger.isEnabledFor(logging.INFO):
        _access_logger.info(record.format(), extra=record.as_extra())


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
    _started_at:    float     = field(default_factory=time.monotonic, repr=False)

    @classmethod
    def from_scope(cls, scope: dict) -> 'AccessLogRecord':
        client = scope.get('client') or ['-']
        return cls(
            client_ip    = str(client[0]),
            method       = scope.get('method', '-'),
            path         = scope.get('path', '-'),
            http_version = scope.get('http_version', '-'),
        )

    def duration_ms(self) -> float:
        return (time.monotonic() - self._started_at) * 1000

    def format(self) -> str:
        if self.close_code is not None:
            return (f'{self.client_ip} '
                    f'"{self.method} {self.path} WS/{self.http_version}" '
                    f'101 close={self.close_code} '
                    f'{self.duration_ms():.0f}ms')
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
    """Wrap *send* to update *record* with status and response size as events flow through."""
    async def capturing_send(event, *args, **kwargs):
        if isinstance(event, dict):
            if event.get('type') == ASGIEvent.HTTP_RESPONSE_START:
                record.status = event.get('status', '-')
            elif event.get('type') == ASGIEvent.HTTP_RESPONSE_BODY:
                record.response_bytes += len(event.get('body', b''))
        elif isinstance(event, bytes) and args:
            # _wrap_send calls send(body, status, headers) for simplified handlers
            record.status = int(args[0])
            record.response_bytes += len(event)
        await send(event, *args, **kwargs)
    return capturing_send
