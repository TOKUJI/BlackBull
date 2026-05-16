"""HTTP/2 Actor classes for the BlackBull Actor model (Phase 6 Step 4).

HTTP2Actor drives the HTTP/2 connection state machine for one TCP connection.
StreamActor owns the lifetime of a single HTTP/2 stream.
"""
import asyncio
import logging
from collections.abc import Awaitable, Callable
from http import HTTPStatus
from typing import Any, Protocol, runtime_checkable
from urllib.parse import urlparse

from ..actor import Actor, Message
from ..event_aggregator import EventAggregator
from .http2_messages import (
    ConnectionAccepted, StreamHeadersReceived, WindowUpdate, Header, Data,
    SettingsReceived, Goaway, WindowRequested,
)
from ..logger import log
from ..protocol.frame import FrameFactory
from ..protocol.frame_types import ErrorCodes, FrameBase, FrameTypes
from ..protocol.stream import Stream, StreamState
from .headers import Headers
from .parser import ParserFactory
from .recipient import AbstractReader, HTTP2Recipient, IncompleteReadError, RecipientFactory
from .response import ResponderFactory
from .sender import AbstractWriter, SenderFactory
from .access_log import AccessLogRecord, _make_capturing_send, _make_disconnect_detecting_receive
from .constants import ASGIEvent
from .http1_actor import RequestActor

logger = logging.getLogger(__name__)


@runtime_checkable
class _StreamRecipient(Protocol):
    """Duck-type interface shared by HTTP2Recipient and HTTP2WSReader."""
    def put_disconnect(self) -> None: ...
    def put_DATAFrame(self, frame) -> bool: ...


def _signal_recipients(recipients: dict[int, _StreamRecipient]) -> None:
    """Inject http.disconnect into every active stream recipient."""
    for recipient in recipients.values():
        recipient.put_disconnect()


def _make_log_record(scope):
    return AccessLogRecord.from_scope(scope)
_access_logger = logging.getLogger('blackbull.access')

_DEFAULT_PRIORITY: dict[str, int | bool] = {'urgency': 3, 'incremental': False}


# ---------------------------------------------------------------------------
# Level A message types (Actor inbox protocol)
# ---------------------------------------------------------------------------



# ---------------------------------------------------------------------------
# Priority helper (mirrors server.py; consolidated in a later step)
# ---------------------------------------------------------------------------

def _resolve_priority(stream: 'Stream', scope: dict) -> dict[str, int | bool]:
    if stream.priority_hint is not None:
        return stream.priority_hint
    raw = scope.get('headers', Headers([])).get(b'priority', b'')
    if raw:
        from ..protocol.frame_types import parse_priority_field
        return parse_priority_field(raw.decode('ascii', errors='replace'))
    return dict(_DEFAULT_PRIORITY)


# ---------------------------------------------------------------------------
# StreamActor — single HTTP/2 stream lifetime
# ---------------------------------------------------------------------------

class StreamActor(Actor):
    """Owns one HTTP/2 stream.

    Single-shot like RequestActor: run() processes one stream and returns.
    Delegates to RequestActor for ASGI dispatch.
    Supervisor strategy: isolate — RST_STREAM on unhandled error.
    """

    def __init__(
        self,
        stream_id: int,
        scope: dict[str, Any],
        receive: Callable[..., Awaitable[Any]],
        send: Callable[..., Awaitable[Any]],
        app: Callable[..., Awaitable[None]],
        aggregator: EventAggregator,
        http2_actor: 'HTTP2Actor',
        log_record,
    ) -> None:
        super().__init__()
        self._stream_id = stream_id
        self._scope = scope
        self._receive = receive
        self._send = send
        self._app = app
        self._aggregator = aggregator
        self._http2_actor = http2_actor
        self._log_record = log_record

    async def run(self) -> None:
        try:
            await RequestActor(
                self._scope, self._receive, self._send,
                self._app, self._aggregator,
            ).run()
        except BaseException:
            await self._http2_actor.send_frame(
                self._http2_actor.factory.rst_stream(
                    self._stream_id, ErrorCodes.INTERNAL_ERROR)
            )
        finally:
            _access_logger.info(
                self._log_record.format(), extra=self._log_record.as_extra())

    async def _handle(self, msg: Message) -> None:  # never reached
        raise NotImplementedError


# ---------------------------------------------------------------------------
# HTTP2Actor — HTTP/2 connection state machine
# ---------------------------------------------------------------------------

class HTTP2Actor(Actor):
    """Drives the HTTP/2 connection state machine for one connection.

    Supervisor strategy: propagate — framing errors send GOAWAY and raise,
    surfacing to the caller.

    If *aggregator* is ``None`` the actor uses the legacy direct-dispatcher
    path (same behaviour as the pre-Actor HTTP2Handler).
    """

    def __init__(
        self,
        reader: 'AbstractReader | None',
        writer: AbstractWriter,
        app: Callable[..., Awaitable[None]],
        aggregator: 'EventAggregator | None',
        *,
        peername: tuple[str, int] | None = None,
        sockname: tuple[str, int] | None = None,
        ssl: bool = False,
    ) -> None:
        super().__init__()
        self._reader = reader
        self._writer = writer
        self._app = app
        self._aggregator = aggregator
        self._peername = peername
        self._sockname = sockname
        self._ssl = ssl

        self.app = app
        self.reader = reader

        # HTTP/2 connection state
        self.root_stream = Stream(0, None, 1)
        self.factory = FrameFactory()
        self._control_sender = SenderFactory.http2(writer, self.factory, 0)
        self._senders: dict = {}
        self.max_concurrent_streams: float = float('inf')
        self._next_push_stream_id = 2
        self._task_group: asyncio.TaskGroup | None = None

    # ------------------------------------------------------------------
    # Public helpers (called by ResponderFactory duck-typing)
    # ------------------------------------------------------------------

    def find_stream(self, stream_id: int) -> 'Stream | None':
        if stream_id == 0:
            return self.root_stream
        return self.root_stream.find_child(stream_id)

    def _allocate_push_stream_id(self) -> int:
        sid = self._next_push_stream_id
        self._next_push_stream_id += 2
        return sid

    def make_sender(self, stream_id: int):
        if stream_id not in self._senders:
            self._senders[stream_id] = SenderFactory.http2(
                self._writer, self.factory, stream_id,
                push_callback=self._handle_push,
            )
        return self._senders[stream_id]

    def _fill_scope_connection(self, scope: dict) -> None:
        """Inject peername/sockname into a freshly-parsed HTTP/2 scope."""
        if self._peername:
            scope['client'] = list(self._peername[:2])
        if self._sockname:
            scope['server'] = list(self._sockname[:2])

    @log
    async def send_frame(self, frame: FrameBase) -> None:
        """Send a raw HTTP/2 frame via the control-plane sender."""
        await self._control_sender(frame)

    async def receive(self) -> bytes:
        """Read one HTTP/2 frame from the connection."""
        assert self._reader is not None, "receive() called with no reader"
        try:
            data = await self._reader.readexactly(9)
        except (IncompleteReadError, asyncio.IncompleteReadError):
            return b''
        size = int.from_bytes(data[:3], 'big', signed=False)
        if size:
            try:
                data += await self._reader.readexactly(size)
            except (IncompleteReadError, asyncio.IncompleteReadError):
                return b''
        return data

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    async def run(self) -> None:
        """HTTP/2 connection state machine — process frames until connection closes."""
        await self.send_frame(self.factory.settings(enable_connect_protocol=True))
        logger.info(self.factory.settings(enable_connect_protocol=True))

        recipients: dict[int, _StreamRecipient] = {}

        async with asyncio.TaskGroup() as tg:
            self._task_group = tg
            await self._frame_loop(tg, recipients)

        self._task_group = None

    async def _frame_loop(
        self,
        tg: asyncio.TaskGroup,
        recipients: dict[int, _StreamRecipient],
    ) -> None:
        """Read frames and dispatch stream tasks until EOF or GOAWAY."""
        waiting_continuation = False
        header_frame = None

        while data := await self.receive():
            frame = self.factory.load(data)
            self.root_stream.add_child(frame.stream_id)
            if (stream := self.root_stream.find_child(frame.stream_id)) is None:
                logger.error('Stream %d not found.', frame.stream_id)
                raise Exception('Unused stream identifier')

            send = self.make_sender(stream.stream_id)
            last_stream_id = self.root_stream.max_stream_id()

            match frame.FrameType():
                case FrameTypes.HEADERS:
                    spawned = await self._on_headers_frame(frame, stream, send, tg, recipients)
                    if not spawned:
                        waiting_continuation = True
                        header_frame = frame
                case FrameTypes.CONTINUATION:
                    spawned = await self._on_continuation_frame(
                        frame, stream, send, tg, recipients, header_frame, waiting_continuation)
                    if spawned:
                        waiting_continuation = False
                case FrameTypes.DATA:
                    await self._on_data_frame(frame, stream, recipients)
                case FrameTypes.GOAWAY:
                    await self._on_goaway_frame(last_stream_id, recipients)
                    return
                case _:
                    await ResponderFactory.create(frame).respond(self)

        _signal_recipients(recipients)

    def _legacy_spawn(
        self,
        tg: asyncio.TaskGroup,
        scope: dict,
        recipient: HTTP2Recipient,
        send,
        log_record,
    ) -> None:
        """Spawn ASGI app task via legacy _run_with_log path (aggregator=None)."""
        from .server import _run_with_log  # noqa: PLC0415
        dispatcher = getattr(self.app, '_dispatcher', None)
        tg.create_task(
            _run_with_log(
                self.app(scope, recipient, send),
                log_record,
                dispatcher=dispatcher,
                scope=scope,
            )
        )

    def _spawn_stream_task(
        self,
        tg: asyncio.TaskGroup,
        stream_id: int,
        scope: dict,
        recipient,
        send,
        log_record,
    ) -> None:
        """Spawn a StreamActor (aggregator path) or legacy _run_with_log task."""
        if self._aggregator is not None:
            stream_actor = StreamActor(
                stream_id=stream_id,
                scope=scope,
                receive=_make_disconnect_detecting_receive(
                    recipient, scope, self._aggregator),
                send=send,
                app=self.app,
                aggregator=self._aggregator,
                http2_actor=self,
                log_record=log_record,
            )
            tg.create_task(stream_actor.run())
        else:
            self._legacy_spawn(tg, scope, recipient, send, log_record)

    async def _on_headers_frame(
        self,
        frame,
        stream: 'Stream',
        send,
        tg: asyncio.TaskGroup,
        recipients: dict[int, _StreamRecipient],
    ) -> bool:
        """Handle a HEADERS frame; return True if stream task spawned, False if awaiting CONTINUATION."""
        if len(self.root_stream.get_children()) >= self.max_concurrent_streams:
            await self.send_frame(
                self.factory.rst_stream(stream.stream_id, ErrorCodes.REFUSED_STREAM))

        if not frame.end_headers:
            return False

        scope = ParserFactory.Get(frame, stream).parse()
        self._fill_scope_connection(scope)
        stream.scope = scope

        if scope.get('type') == 'websocket':
            # RFC 8441 — Extended CONNECT bootstrapping WebSocket over HTTP/2
            stream.on_headers_received(end_stream=False)
            log_record = _make_log_record(scope)
            await self._handle_h2_websocket(stream, tg, recipients, log_record)
            return True

        scope['http2_priority'] = _resolve_priority(stream, scope)
        scope['extensions'] = {ASGIEvent.HTTP_RESPONSE_PUSH: {}}
        stream_recipient = RecipientFactory.http2()
        recipients[stream.stream_id] = stream_recipient
        stream.on_headers_received(end_stream=bool(frame.end_stream))
        if frame.end_stream:
            stream_recipient.put_event(
                {'type': ASGIEvent.HTTP_REQUEST, 'body': b'', 'more_body': False})
        log_record = _make_log_record(scope)
        capturing_send = _make_capturing_send(send, log_record)
        self._spawn_stream_task(tg, stream.stream_id, scope, stream_recipient, capturing_send, log_record)
        return True

    async def _on_continuation_frame(
        self,
        frame,
        stream: 'Stream',
        send,
        tg: asyncio.TaskGroup,
        recipients: dict[int, _StreamRecipient],
        header_frame,
        waiting_continuation: bool,
    ) -> bool:
        """Handle a CONTINUATION frame; return True if stream task spawned, False if still accumulating."""
        if not waiting_continuation:
            logger.error('Unexpected CONTINUATION without preceding HEADERS.')
            await self.send_frame(
                self.factory.goaway(self.root_stream.max_stream_id()))

        assert header_frame is not None
        header_frame.raw_block += frame.payload

        if not frame.end_headers:
            return False

        header_frame.parse_payload()
        scope = ParserFactory.Get(header_frame, stream).parse()
        self._fill_scope_connection(scope)
        scope['http2_priority'] = _resolve_priority(stream, scope)
        scope['extensions'] = {ASGIEvent.HTTP_RESPONSE_PUSH: {}}
        stream.scope = scope
        stream_recipient = RecipientFactory.http2()
        recipients[stream.stream_id] = stream_recipient
        log_record = _make_log_record(scope)
        capturing_send = _make_capturing_send(send, log_record)
        self._spawn_stream_task(tg, stream.stream_id, scope, stream_recipient, capturing_send, log_record)
        return True

    async def _on_data_frame(
        self,
        frame,
        stream: 'Stream',
        recipients: dict[int, _StreamRecipient],
    ) -> None:
        """Handle a DATA frame: deliver to the stream's recipient then issue WINDOW_UPDATE.

        WINDOW_UPDATE is sent only after successful delivery so that a full
        recipient queue withholds flow-control credit instead of silently
        accepting data the app cannot process (RFC 7540 §6.9).
        """
        if stream.state in (StreamState.HALF_CLOSED_REMOTE, StreamState.CLOSED):
            await self.send_frame(
                self.factory.rst_stream(stream.stream_id, ErrorCodes.STREAM_CLOSED))
            return

        stream.on_data_received(end_stream=bool(frame.end_stream))
        if stream.stream_id in recipients:
            delivered = recipients[stream.stream_id].put_DATAFrame(frame)
            if delivered:
                await self.send_frame(
                    self.factory.window_update(stream.stream_id, frame.length))
            else:
                await self.send_frame(
                    self.factory.rst_stream(stream.stream_id, ErrorCodes.ENHANCE_YOUR_CALM))
        else:
            logger.warning('DATA for stream %d but no recipient found', stream.stream_id)

    async def _on_goaway_frame(
        self,
        last_stream_id: int,
        recipients: dict[int, _StreamRecipient],
    ) -> None:
        """Handle an incoming GOAWAY: echo one back and signal all recipients."""
        await self.send_frame(self.factory.goaway(last_stream_id))
        _signal_recipients(recipients)

    async def _handle_h2_websocket(
        self,
        stream: 'Stream',
        tg: asyncio.TaskGroup,
        recipients: dict[int, _StreamRecipient],
        log_record,
    ) -> None:
        """Bootstrap a WebSocket connection over HTTP/2 per RFC 8441.

        Stores a deferred _ws_send_200 callback under the same scope key
        (_ws_send_101) that WebSocketActor._send() already reads, so that
        WebSocketActor can be reused without modification.  The 200 HEADERS
        response (and optional sec-websocket-protocol) is sent when the ASGI
        app calls websocket.accept.
        """
        from uuid import uuid4  # noqa: PLC0415
        from .websocket_actor import WebSocketActor  # noqa: PLC0415
        from .http2_ws import HTTP2WSReader, HTTP2WSWriter  # noqa: PLC0415

        scope = stream.scope
        assert scope is not None
        scope['_connection_id'] = str(uuid4())
        stream_send = self.make_sender(stream.stream_id)

        async def _ws_send_200(subprotocol=None):
            headers = []
            if subprotocol:
                sp = subprotocol if isinstance(subprotocol, str) else subprotocol.decode()
                headers = [(b'sec-websocket-protocol', sp.encode())]
            await stream_send({'type': 'http.response.start', 'status': 200, 'headers': headers})

        scope['_ws_send_101'] = _ws_send_200  # WebSocketActor calls this on websocket.accept

        ws_reader = HTTP2WSReader()
        ws_writer = HTTP2WSWriter(stream_send)
        recipients[stream.stream_id] = ws_reader  # DATA frames routed here

        aggregator = self._aggregator
        if aggregator is None:
            from ..event import EventDispatcher  # noqa: PLC0415
            from ..event_aggregator import EventAggregator  # noqa: PLC0415
            aggregator = EventAggregator(EventDispatcher())

        log_record.status = 200
        ws_actor = WebSocketActor(
            ws_reader, ws_writer, scope, self.app, aggregator,
            peername=self._peername, sockname=self._sockname, ssl=self._ssl,
        )

        async def _run_ws():
            try:
                await ws_actor.run()
            finally:
                log_record.close_code = ws_actor._disconnect_code
                _access_logger.info(log_record.format(), extra=log_record.as_extra())

        tg.create_task(_run_ws())

    async def _handle_push(self, event: dict, parent_stream_id: int) -> None:
        """Handle an 'http.response.push' ASGI event (RFC 7540 §8.2)."""
        from urllib.parse import urlparse as _urlparse  # noqa: PLC0415

        push_stream_id = self._allocate_push_stream_id()
        path = event.get('path', '/')

        parent_stream = self.root_stream.find_child(parent_stream_id)
        parent_scope = (parent_stream.scope
                        if (parent_stream and parent_stream.scope is not None) else {})
        raw_authority = (
            parent_scope.get('headers', Headers([])).get(b':authority') or
            parent_scope.get('headers', Headers([])).get(b'host') or
            b'localhost')
        authority = raw_authority.decode() if isinstance(raw_authority, bytes) else raw_authority

        from ..protocol.frame_types import PseudoHeaders  # noqa: PLC0415
        pseudo = {
            PseudoHeaders.METHOD:    'GET',
            PseudoHeaders.PATH:      path,
            PseudoHeaders.SCHEME:    parent_scope.get('scheme', 'https'),
            PseudoHeaders.AUTHORITY: authority,
        }
        regular = [
            (k.decode() if isinstance(k, bytes) else k,
             v.decode() if isinstance(v, bytes) else v)
            for k, v in event.get('headers', [])
            if not (k.decode() if isinstance(k, bytes) else k).startswith(':')
        ]

        pp = self.factory.push_promise(parent_stream_id, push_stream_id, pseudo, regular)
        await self.send_frame(pp)

        query = _urlparse(path).query
        pushed_scope: dict = {
            'type': 'http',
            'http_version': '2',
            'method': 'GET',
            'path': path,
            'scheme': parent_scope.get('scheme', 'https'),
            'query_string': query.encode() if query else b'',
            'root_path': '',
            'client': parent_scope.get('client'),
            'headers': Headers([(k.encode() if isinstance(k, str) else k,
                                  v.encode() if isinstance(v, str) else v)
                                 for k, v in regular]),
            'extensions': {ASGIEvent.HTTP_RESPONSE_PUSH: {}},
            'http2_priority': _DEFAULT_PRIORITY,
        }

        push_recipient = RecipientFactory.http2()
        push_recipient.put_event({'type': ASGIEvent.HTTP_REQUEST, 'body': b'', 'more_body': False})
        push_sender = SenderFactory.http2(
            self._writer, self.factory, push_stream_id, push_callback=None)
        log_record = _make_log_record(pushed_scope)
        capturing_send = _make_capturing_send(push_sender, log_record)

        if self._task_group is not None:
            self._spawn_stream_task(
                self._task_group, push_stream_id, pushed_scope,
                push_recipient, capturing_send, log_record,
            )

    async def _handle(self, msg: Message) -> None:  # never reached
        raise NotImplementedError
