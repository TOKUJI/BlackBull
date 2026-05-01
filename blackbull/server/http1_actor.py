"""HTTP/1.1 Actor classes for the BlackBull Actor model (Phase 6 Step 3).

HTTP1Actor drives the keep-alive loop for one TCP connection.
RequestActor owns the lifetime of a single HTTP request.
"""
import logging
import re
import time
from base64 import b64encode
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from hashlib import sha1
from http import HTTPStatus
from typing import Any
from urllib.parse import urlparse

from ..actor import Actor, Message
from ..event import Event
from ..event_aggregator import EventAggregator
from .headers import Headers
from .recipient import AbstractReader, IncompleteReadError, RecipientFactory
from .sender import AbstractWriter, SenderFactory

logger = logging.getLogger(__name__)
_access_logger = logging.getLogger('blackbull.access')

_REQ_END = b'\r\n\r\n'


# ---------------------------------------------------------------------------
# Access-log helpers (mirrored from server.py; consolidated in a later step)
# ---------------------------------------------------------------------------

@dataclass
class _AccessLogRecord:
    client_ip:      str
    method:         str
    path:           str
    http_version:   str
    status:         int | str = '-'
    response_bytes: int = 0
    _started_at:    float = field(default_factory=time.monotonic, repr=False)

    @classmethod
    def from_scope(cls, scope: dict) -> '_AccessLogRecord':
        client = scope.get('client') or ['-']
        return cls(
            client_ip=str(client[0]),
            method=scope.get('method', '-'),
            path=scope.get('path', '-'),
            http_version=scope.get('http_version', '-'),
        )

    def duration_ms(self) -> float:
        return (time.monotonic() - self._started_at) * 1000

    def format(self) -> str:
        return (f'{self.client_ip} '
                f'"{self.method} {self.path} HTTP/{self.http_version}" '
                f'{self.status} {self.response_bytes} '
                f'{self.duration_ms():.0f}ms')

    def as_extra(self) -> dict:
        return {
            'client_ip':      self.client_ip,
            'method':         self.method,
            'path':           self.path,
            'http_version':   self.http_version,
            'status':         self.status,
            'response_bytes': self.response_bytes,
            'duration_ms':    self.duration_ms(),
        }


def _make_capturing_send(send, record: _AccessLogRecord):
    async def capturing_send(event, *args, **kwargs):
        if isinstance(event, dict):
            if event.get('type') == 'http.response.start':
                record.status = event.get('status', '-')
            elif event.get('type') == 'http.response.body':
                record.response_bytes += len(event.get('body', b''))
        await send(event, *args, **kwargs)
    return capturing_send


def _make_disconnect_detecting_receive(receive, scope: dict, aggregator: 'EventAggregator'):
    async def detecting_receive():
        event = await receive()
        if isinstance(event, dict) and event.get('type') == 'http.disconnect':
            if not scope.get('_disconnected'):
                scope['_disconnected'] = True
                await aggregator.on_request_disconnected(scope)
        return event
    return detecting_receive


# ---------------------------------------------------------------------------
# RequestActor — single HTTP request lifetime
# ---------------------------------------------------------------------------

class RequestActor(Actor):
    """Owns one HTTP/1.1 request lifetime.

    Spawned by HTTP1Actor, awaited to completion.  Calls the ASGI app and
    emits before_handler / after_handler / request_completed / error via
    EventAggregator.
    """

    def __init__(
        self,
        scope: dict[str, Any],
        receive: Callable[..., Awaitable[Any]],
        send: Callable[..., Awaitable[Any]],
        app: Callable[..., Awaitable[None]],
        aggregator: EventAggregator,
    ) -> None:
        super().__init__()
        self._scope = scope
        self._receive = receive
        self._send = send
        self._app = app
        self._aggregator = aggregator

    async def run(self) -> None:  # override: single-shot, no inbox loop
        await self._aggregator.on_request_received(self._scope)
        exc: BaseException | None = None
        try:
            await self._aggregator.on_before_handler(
                self._scope, self._receive, self._send,
                call_next=self._call_app,
            )
        except BaseException as e:
            exc = e
            await self._aggregator.on_error(self._scope, e)
            raise
        finally:
            await self._aggregator.on_after_handler(self._scope, exception=exc)
            if exc is None:
                await self._aggregator.on_request_completed(self._scope)

    async def _call_app(
        self,
        scope: dict[str, Any],
        receive: Callable[..., Awaitable[Any]],
        send: Callable[..., Awaitable[Any]],
    ) -> None:
        await self._app(scope, receive, send)

    async def _handle(self, msg: Message) -> None:  # never reached
        raise NotImplementedError


# ---------------------------------------------------------------------------
# HTTP1Actor — keep-alive connection loop
# ---------------------------------------------------------------------------

class HTTP1Actor(Actor):
    """Drives the HTTP/1.1 keep-alive loop for one connection.

    Supervisor strategy: isolate — an unhandled exception from a RequestActor
    closes the connection without crashing sibling connections.

    If *aggregator* is ``None`` the actor falls back to the legacy direct-
    dispatcher path (same behaviour as the pre-Actor HTTP11Handler), so that
    HTTP11Handler can delegate here without requiring a production-ready
    EventAggregator.
    """

    def __init__(
        self,
        reader: AbstractReader,
        writer: AbstractWriter,
        app: Callable[..., Awaitable[None]],
        aggregator: 'EventAggregator | None',
        *,
        request: bytes = b'',
        peername: tuple | None = None,
        sockname: tuple | None = None,
        ssl: bool = False,
    ) -> None:
        super().__init__()
        self._reader = reader
        self._writer = writer
        self._app = app
        self._aggregator = aggregator
        self._request = request
        self._peername = peername
        self._sockname = sockname
        self._ssl = ssl

    async def run(self) -> None:
        """Keep-alive loop — process requests until connection closes."""
        send = SenderFactory.http1(self._writer)
        try:
            while True:
                while not self._request.endswith(_REQ_END):
                    self._request += await self._reader.readuntil(_REQ_END)

                scope = self._parse(self._request)
                self._fill_connection_info(scope)

                if scope.get('type') == 'websocket':
                    await self._handle_upgrade(scope)
                    return

                if scope['headers'].get(b'expect').lower() == b'100-continue':
                    await send(b'', HTTPStatus.CONTINUE)

                log_record = _AccessLogRecord.from_scope(scope)
                scope['state']['access_log'] = log_record
                capturing_send = _make_capturing_send(send, log_record)
                inner_receive = RecipientFactory.http1(self._reader, scope)

                if self._aggregator is not None:
                    detecting_receive = _make_disconnect_detecting_receive(
                        inner_receive, scope, self._aggregator)
                    request_actor = RequestActor(
                        scope, detecting_receive, capturing_send,
                        self._app, self._aggregator,
                    )
                    try:
                        await request_actor.run()
                    except BaseException:
                        break  # isolate: close connection on unhandled error
                    finally:
                        _access_logger.info(log_record.format(), extra=log_record.as_extra())
                else:
                    # Legacy path — mirrors existing HTTP11Handler behaviour
                    _dispatcher = getattr(self._app, '_dispatcher', None)
                    if _dispatcher is not None:
                        detecting_receive = self._make_legacy_disconnect_receive(
                            inner_receive, scope, _dispatcher, log_record)
                    else:
                        detecting_receive = inner_receive
                    try:
                        if _dispatcher is not None:
                            await _dispatcher.emit(Event(
                                'request_received',
                                detail={
                                    'scope':        scope,
                                    'client_ip':    log_record.client_ip,
                                    'method':       log_record.method,
                                    'path':         log_record.path,
                                    'http_version': log_record.http_version,
                                    'headers':      scope.get('headers', []),
                                },
                            ))
                        await self._app(scope, detecting_receive, capturing_send)
                    finally:
                        _access_logger.info(log_record.format(), extra=log_record.as_extra())
                        if (_dispatcher is not None
                                and scope.get('type') == 'http'
                                and not scope.get('_disconnected')):
                            await _dispatcher.emit(Event(
                                'request_completed',
                                detail={
                                    'scope':          scope,
                                    'client_ip':      log_record.client_ip,
                                    'method':         log_record.method,
                                    'path':           log_record.path,
                                    'http_version':   log_record.http_version,
                                    'status':         log_record.status,
                                    'response_bytes': log_record.response_bytes,
                                    'duration_ms':    log_record.duration_ms(),
                                },
                            ))

                self._request = b''
                next_chunk = await self._reader.readuntil(_REQ_END)
                if next_chunk == _REQ_END:
                    break
                self._request = next_chunk

        except IncompleteReadError:
            await send({'type': 'http.disconnect'})

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _parse(self, data: bytes) -> dict:
        """Parse raw HTTP/1.1 request bytes into an ASGI scope dict."""
        lines = data.split(b'\r\n')
        method, path, version = lines[0].split(b' ')
        path_parsed = urlparse(path)

        raw: list[tuple[bytes, bytes]] = []
        for line in lines[1:]:
            line = line.strip()
            if b':' in line:
                key, value = line.split(b':', 1)
                raw.append((key.lower(), value.strip()))
        headers = Headers(raw)

        scope = {
            'type': 'http',
            'asgi': {'version': '3.0', 'spec_version': '2.0'},
            'http_version': re.sub(r'HTTP/(.*)', r'\1', version.decode('utf-8')),
            'method': method.decode('utf-8'),
            'scheme': 'http',
            'path': path_parsed.path.decode('utf-8'),
            'raw_path': path,
            'query_string': path_parsed.query,
            'root_path': headers.get(b'X-Forwarded-Prefix', b'').decode('utf-8'),
            'headers': headers,
            'client': None,
            'server': None,
            'state': {},
        }

        if headers.getlist(b'host'):
            host, port = headers.get(b'host').split(b':')
            scope['server'] = [host.decode('utf-8'), int(port)]

        if headers.getlist(b'upgrade'):
            scope['type'] = headers.get(b'upgrade').decode('utf-8').lower()
            if scope['type'] == 'websocket':
                scope['scheme'] = 'ws'
        elif headers.getlist(b'connection'):
            scope['scheme'] = headers.get(b'connection').decode('utf-8').lower()

        return scope

    def _fill_connection_info(self, scope: dict) -> None:
        if self._peername:
            scope['client'] = list(self._peername[:2])
        if self._sockname and scope['server'] is None:
            scope['server'] = list(self._sockname[:2])
        if self._ssl:
            scope['scheme'] = 'wss' if scope.get('type') == 'websocket' else 'https'

    async def _handle_upgrade(self, scope: dict) -> None:
        """Delegate WebSocket upgrade to WebSocketHandler (replaced in Step 5)."""
        # Lazy import avoids circular dependency with server.py
        from .server import WebSocketHandler  # noqa: PLC0415
        log_record = _AccessLogRecord.from_scope(scope)
        log_record.status = HTTPStatus.SWITCHING_PROTOCOLS
        try:
            await WebSocketHandler(self._app, self._reader, self._writer, scope).run()
        finally:
            _access_logger.info(log_record.format(), extra=log_record.as_extra())

    @staticmethod
    def _make_legacy_disconnect_receive(receive, scope: dict, dispatcher, log_record):
        """Legacy disconnect-detecting receive wrapper (mirrors server.py helper)."""
        async def detecting_receive():
            event = await receive()
            if isinstance(event, dict) and event.get('type') == 'http.disconnect':
                if not scope.get('_disconnected'):
                    scope['_disconnected'] = True
                    await dispatcher.emit(Event(
                        'request_disconnected',
                        detail={
                            'scope':        scope,
                            'client_ip':    log_record.client_ip,
                            'method':       log_record.method,
                            'path':         log_record.path,
                            'http_version': log_record.http_version,
                        },
                    ))
            return event
        return detecting_receive

    def _should_keep_alive(self, scope: dict) -> bool:
        """Return True if the connection should persist after this request."""
        http_version = scope.get('http_version', '1.0')
        connection = scope['headers'].get(b'connection', b'').lower()
        if http_version == '1.1':
            return connection != b'close'
        return connection == b'keep-alive'

    async def _handle(self, msg: Message) -> None:  # never reached
        raise NotImplementedError
