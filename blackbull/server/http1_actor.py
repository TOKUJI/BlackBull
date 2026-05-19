"""HTTP/1.1 Actor classes for the BlackBull Actor model (Phase 6 Step 3).

HTTP1Actor drives the keep-alive loop for one TCP connection.
RequestActor owns the lifetime of a single HTTP request.
"""
import logging
import re
from base64 import b64encode
from collections.abc import Awaitable, Callable
from hashlib import sha1
from http import HTTPStatus
from typing import Any
from urllib.parse import urlparse

from ..actor import Actor, Message
from ..event import Event
from ..event_aggregator import EventAggregator
from .headers import Headers
from .recipient import AbstractReader, IncompleteReadError, RecipientFactory, _WS_EVENT_QUEUE_DEPTH
from .sender import AbstractWriter, SenderFactory
from .access_log import AccessLogRecord as _AccessLogRecord, _make_capturing_send, _make_disconnect_detecting_receive
from .constants import ASGIEvent, WSCloseCode

logger = logging.getLogger(__name__)
_access_logger = logging.getLogger('blackbull.access')

_REQ_END = b'\r\n\r\n'
_WS_GUID = b'258EAFA5-E914-47DA-95CA-C5AB0DC85B11'  # RFC 6455 §1.3
_HTTP_PORT  = 80
_HTTPS_PORT = 443


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
    dispatcher path (fires events via ``app._dispatcher`` directly), so that
    BlackBull apps without a full EventAggregator still receive lifecycle events.
    """

    def __init__(
        self,
        reader: AbstractReader,
        writer: AbstractWriter,
        app: Callable[..., Awaitable[None]],
        aggregator: 'EventAggregator | None',
        *,
        request: bytes = b'',
        peername: tuple[str, int] | None = None,
        sockname: tuple[str, int] | None = None,
        ssl: bool = False,
        ws_queue_depth: int = _WS_EVENT_QUEUE_DEPTH,
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
        self._ws_queue_depth = ws_queue_depth

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

                if not await self._dispatch_request(scope, inner_receive, capturing_send, log_record):
                    break  # unhandled error — close connection

                self._request = b''
                next_chunk = await self._reader.readuntil(_REQ_END)
                if next_chunk == _REQ_END:
                    break
                self._request = next_chunk

        except IncompleteReadError:
            await send({'type': ASGIEvent.HTTP_DISCONNECT})

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
            parts = headers.get(b'host').split(b':')
            host = parts[0]
            port = int(parts[1]) if len(parts) > 1 else (_HTTPS_PORT if self._ssl else _HTTP_PORT)
            scope['server'] = [host.decode('utf-8'), port]

        if headers.getlist(b'upgrade'):
            scope['type'] = headers.get(b'upgrade').decode('utf-8').lower()
            if scope['type'] == 'websocket':
                scope['scheme'] = 'ws'

        return scope

    def _fill_connection_info(self, scope: dict) -> None:
        if self._peername is not None:
            scope['client'] = list(self._peername)

        if scope.get('server') is None and self._sockname is not None:
            scope['server'] = list(self._sockname)

        if self._ssl:
            scope['scheme'] = 'wss' if scope.get('type') == 'websocket' else 'https'

    async def _handle_upgrade(self, scope: dict) -> None:
        """Handle WebSocket upgrade."""
        from uuid import uuid4  # noqa: PLC0415
        from .websocket_actor import WebSocketActor  # noqa: PLC0415
        aggregator = self._aggregator
        if aggregator is None:
            # No aggregator — use a silent dispatcher so WebSocketActor can fire
            # lifecycle events without any subscribers receiving them.
            from ..event import EventDispatcher  # noqa: PLC0415
            from ..event_aggregator import EventAggregator  # noqa: PLC0415
            aggregator = EventAggregator(EventDispatcher())

        log_record = _AccessLogRecord.from_scope(scope)
        log_record.status = 101  # HTTP 101 Switching Protocols

        if not await self._do_ws_handshake(scope):
            return  # version check failed; 400 already sent
        scope['_connection_id'] = str(uuid4())
        ws_actor = WebSocketActor(
            self._reader, self._writer, scope, self._app, aggregator,
            peername=self._peername, sockname=self._sockname, ssl=self._ssl,
            ws_queue_depth=self._ws_queue_depth,
        )
        try:
            await ws_actor.run()
        finally:
            log_record.close_code = ws_actor._disconnect_code
            _access_logger.info(log_record.format(), extra=log_record.as_extra())

    async def _do_ws_handshake(self, scope: dict) -> bool:
        """Validate the WebSocket upgrade and store a deferred 101 callback.

        Returns True if the handshake is valid and ready to proceed, False if
        a 400 Bad Request was already sent (bad Sec-WebSocket-Version).

        The actual HTTP 101 response is deferred: it is sent by
        WebSocketActor._send when the ASGI app calls websocket.accept, so that
        the chosen subprotocol from that event can be included in the 101 headers
        (RFC 6455 §4.2.2).
        """
        send = SenderFactory.http1(self._writer)
        headers = scope.get('headers', Headers([]))
        key = headers.get(b'sec-websocket-key', b'')
        accept_key = b64encode(sha1(key + _WS_GUID).digest())
        version = headers.get(b'sec-websocket-version', b'')
        if version != b'13':
            await send(b'', HTTPStatus.BAD_REQUEST,
                       [(b'sec-websocket-version', b'13')])
            return False

        # Populate scope['subprotocols'] per ASGI WebSocket spec
        raw_sp = headers.get(b'sec-websocket-protocol', b'')
        client_protos = (
            [p.strip().decode('utf-8', errors='replace') for p in raw_sp.split(b',')]
            if raw_sp else []
        )
        scope['subprotocols'] = client_protos

        # Auto-negotiate from app.available_ws_protocols (backward-compat fallback).
        # This is used when the handler calls websocket.accept without a subprotocol.
        available_raw = getattr(self._app, 'available_ws_protocols', [])
        available = {(p.decode('utf-8', errors='replace') if isinstance(p, bytes) else p)
                     for p in available_raw}
        auto_subprotocol = next((p for p in client_protos if p in available), None)
        scope['_ws_auto_subprotocol'] = auto_subprotocol

        # RFC 7692 permessage-deflate negotiation.  Cached on the scope so
        # WebSocketActor can pick it up after the handshake commits, and
        # echoed back as ``Sec-WebSocket-Extensions`` in the 101 response.
        from ..env import get_settings as _get_settings  # noqa: PLC0415
        from .permessage_deflate import negotiate as _negotiate_deflate  # noqa: PLC0415
        deflate_params = None
        deflate_response = None
        if _get_settings().ws_permessage_deflate:
            offer = headers.get(b'sec-websocket-extensions', b'')
            deflate_params, deflate_response = _negotiate_deflate(offer or None)
        scope['_ws_deflate'] = deflate_params

        async def _send_101(subprotocol=None):
            hs_headers = Headers([
                (b'upgrade', b'websocket'),
                (b'connection', b'upgrade'),
                (b'sec-websocket-accept', accept_key),
            ])
            if subprotocol:
                sp = subprotocol.encode() if isinstance(subprotocol, str) else subprotocol
                hs_headers.append(b'sec-websocket-protocol', sp)
            if deflate_response is not None:
                hs_headers.append(b'sec-websocket-extensions', deflate_response)
            await send(b'', HTTPStatus.SWITCHING_PROTOCOLS, hs_headers)

        scope['_ws_send_101'] = _send_101
        return True

    @staticmethod
    def _make_legacy_disconnect_receive(receive, scope: dict, dispatcher, log_record):
        """Legacy disconnect-detecting receive wrapper (mirrors server.py helper)."""
        async def detecting_receive():
            event = await receive()
            if isinstance(event, dict) and event.get('type') == ASGIEvent.HTTP_DISCONNECT:
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

    async def _dispatch_request(
        self,
        scope: dict,
        inner_receive,
        capturing_send,
        log_record,
    ) -> bool:
        """Dispatch one HTTP request via the aggregator or legacy path.

        Returns True on success, False if an unhandled error should close the connection.
        """
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
                return False
            finally:
                _access_logger.info(log_record.format(), extra=log_record.as_extra())
        else:
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
        return True

    def _should_keep_alive(self, scope: dict) -> bool:
        """Return True if the connection should persist after this request."""
        http_version = scope.get('http_version', '1.0')
        connection = scope['headers'].get(b'connection', b'').lower()
        if http_version == '1.1':
            return connection != b'close'
        return connection == b'keep-alive'

    async def _handle(self, msg: Message) -> None:  # never reached
        raise NotImplementedError
