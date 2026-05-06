"""HTTP/2 Actor classes for the BlackBull Actor model (Phase 6 Step 4).

HTTP2Actor drives the HTTP/2 connection state machine for one TCP connection.
StreamActor owns the lifetime of a single HTTP/2 stream.
"""
import asyncio
import logging
from dataclasses import dataclass, field
from collections.abc import Awaitable, Callable
from http import HTTPStatus
from typing import Any
from urllib.parse import urlparse

from ..actor import Actor, Message
from ..event_aggregator import EventAggregator
from ..logger import log
from ..protocol.frame import (
    ErrorCodes, FrameBase, FrameFactory, FrameTypes,
    DEFAULT_INITIAL_WINDOW_SIZE,
)
from ..protocol.stream import Stream, StreamState
from .headers import HeaderList, Headers
from .parser import ParserFactory
from .recipient import AbstractReader, HTTP2Recipient, RecipientFactory
from .response import ResponderFactory
from .sender import AbstractWriter, SenderFactory
from .http1_actor import RequestActor

logger = logging.getLogger(__name__)


def _make_capturing_send(send, record):
    """Wrap *send* to capture status and response_bytes into *record*."""
    async def capturing_send(event, *args, **kwargs):
        if isinstance(event, dict):
            if event.get('type') == 'http.response.start':
                record.status = event.get('status', '-')
            elif event.get('type') == 'http.response.body':
                record.response_bytes += len(event.get('body', b''))
        await send(event, *args, **kwargs)
    return capturing_send


def _make_log_record(scope):
    """Create an AccessLogRecord for *scope* (lazy import avoids circular dep)."""
    from .server import AccessLogRecord  # noqa: PLC0415
    return AccessLogRecord.from_scope(scope)
_access_logger = logging.getLogger('blackbull.access')

_DEFAULT_PRIORITY: dict[str, int | bool] = {'urgency': 3, 'incremental': False}


# ---------------------------------------------------------------------------
# Level A message types (Actor inbox protocol)
# ---------------------------------------------------------------------------

@dataclass
class ConnectionAccepted(Message):
    reader: AbstractReader | None = field(default=None, compare=False, repr=False)
    writer: AbstractWriter | None = field(default=None, compare=False, repr=False)
    peername: tuple[str, int] | None = field(default=None, compare=False, repr=False)

@dataclass
class StreamHeadersReceived(Message):
    stream_id: int | None = field(default=None, compare=False, repr=False)
    headers: HeaderList | None = field(default=None, compare=False, repr=False)
    end_stream: bool | None = field(default=None, compare=False, repr=False)

@dataclass
class WindowUpdate(Message):
    stream_id: int | None = field(default=None, compare=False, repr=False)
    increment: int | None = field(default=None, compare=False, repr=False)

@dataclass
class Header(Message):
    stream_id: int | None = field(default=None, compare=False, repr=False)
    headers: HeaderList | None = field(default=None, compare=False, repr=False)
    end_stream: bool | None = field(default=None, compare=False, repr=False)

@dataclass
class Data(Message):
    stream_id: int | None = field(default=None, compare=False, repr=False)
    data: bytes | None = field(default=None, compare=False, repr=False)
    end_stream: bool | None = field(default=None, compare=False, repr=False)

@dataclass
class SettingsReceived(Message):
    settings: dict[int, int] | None = field(default=None, compare=False, repr=False)

@dataclass
class Goaway(Message):
    last_stream_id: int | None = field(default=None, compare=False, repr=False)
    error_code: int | None = field(default=None, compare=False, repr=False)
    debug_data: bytes | None = field(default=None, compare=False, repr=False)

@dataclass
class WindowRequested(Message):
    stream_id: int | None = field(default=None, compare=False, repr=False)
    increment: int | None = field(default=None, compare=False, repr=False)


# ---------------------------------------------------------------------------
# Priority helper (mirrors server.py; consolidated in a later step)
# ---------------------------------------------------------------------------

def _resolve_priority(stream: 'Stream', scope: dict) -> dict[str, int | bool]:
    if stream.priority_hint is not None:
        return stream.priority_hint
    raw = scope.get('headers', Headers([])).get(b'priority', b'')
    if raw:
        from ..protocol.frame import parse_priority_field
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

    @log
    async def send_frame(self, frame: FrameBase) -> None:
        """Send a raw HTTP/2 frame via the control-plane sender."""
        await self._control_sender(frame)

    async def receive(self) -> bytes:
        """Read one HTTP/2 frame from the connection."""
        assert self._reader is not None, "receive() called with no reader"
        data = await self._reader.read(9)
        if not data:
            return b''
        size = int.from_bytes(data[:3], 'big', signed=False)
        data += await self._reader.read(size)
        return data

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    async def run(self) -> None:
        """HTTP/2 connection state machine — process frames until connection closes."""
        await self.send_frame(self.factory.settings())
        logger.info(self.factory.settings())

        recipients: dict[int, HTTP2Recipient] = {}

        async with asyncio.TaskGroup() as tg:
            self._task_group = tg
            await self._frame_loop(tg, recipients)

        self._task_group = None

    async def _frame_loop(
        self,
        tg: asyncio.TaskGroup,
        recipients: dict[int, HTTP2Recipient],
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
                    if len(self.root_stream.get_children()) >= self.max_concurrent_streams:
                        await self.send_frame(
                            self.factory.rst_stream(stream.stream_id, ErrorCodes.REFUSED_STREAM))

                    if frame.end_headers:
                        waiting_continuation = False
                        scope = ParserFactory.Get(frame, stream).parse()
                        scope['http2_priority'] = _resolve_priority(stream, scope)
                        scope['extensions'] = {'http.response.push': {}}
                        stream.scope = scope
                        stream_recipient = RecipientFactory.http2()
                        recipients[stream.stream_id] = stream_recipient
                        stream.on_headers_received(end_stream=bool(frame.end_stream))
                        if frame.end_stream:
                            stream_recipient.put_event(
                                {'type': 'http.request', 'body': b'', 'more_body': False})
                        log_record = _make_log_record(scope)
                        capturing_send = _make_capturing_send(send, log_record)
                        if self._aggregator is not None:
                            stream_actor = StreamActor(
                                stream_id=stream.stream_id,
                                scope=scope,
                                receive=stream_recipient,
                                send=capturing_send,
                                app=self.app,
                                aggregator=self._aggregator,
                                http2_actor=self,
                                log_record=log_record,
                            )
                            tg.create_task(stream_actor.run())
                        else:
                            self._legacy_spawn(tg, scope, stream_recipient, capturing_send, log_record)
                    else:
                        waiting_continuation = True
                        header_frame = frame
                        continue

                case FrameTypes.CONTINUATION:
                    if not waiting_continuation:
                        logger.error('Unexpected CONTINUATION without preceding HEADERS.')
                        await self.send_frame(self.factory.goaway(last_stream_id))

                    assert header_frame is not None
                    header_frame.raw_block += frame.payload

                    if frame.end_headers:
                        waiting_continuation = False
                        header_frame.parse_payload()
                        scope = ParserFactory.Get(header_frame, stream).parse()
                        scope['http2_priority'] = _resolve_priority(stream, scope)
                        scope['extensions'] = {'http.response.push': {}}
                        stream.scope = scope
                        stream_recipient = RecipientFactory.http2()
                        recipients[stream.stream_id] = stream_recipient
                        log_record = _make_log_record(scope)
                        capturing_send = _make_capturing_send(send, log_record)
                        if self._aggregator is not None:
                            stream_actor = StreamActor(
                                stream_id=stream.stream_id,
                                scope=scope,
                                receive=stream_recipient,
                                send=capturing_send,
                                app=self.app,
                                aggregator=self._aggregator,
                                http2_actor=self,
                                log_record=log_record,
                            )
                            tg.create_task(stream_actor.run())
                        else:
                            self._legacy_spawn(tg, scope, stream_recipient, capturing_send, log_record)
                    else:
                        continue

                case FrameTypes.DATA:
                    await self.send_frame(
                        self.factory.window_update(stream.stream_id, frame.length))
                    if stream.state in (StreamState.HALF_CLOSED_REMOTE, StreamState.CLOSED):
                        await self.send_frame(
                            self.factory.rst_stream(stream.stream_id, ErrorCodes.STREAM_CLOSED))
                    else:
                        stream.on_data_received(end_stream=bool(frame.end_stream))
                        if stream.stream_id in recipients:
                            recipients[stream.stream_id].put_DATAFrame(frame)
                        else:
                            logger.warning(
                                'DATA for stream %d but no recipient found', stream.stream_id)

                case FrameTypes.GOAWAY:
                    await self.send_frame(self.factory.goaway(last_stream_id))
                    return

                case _:
                    await ResponderFactory.create(frame).respond(self)

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

        from ..protocol.frame import PseudoHeaders  # noqa: PLC0415
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
            'extensions': {'http.response.push': {}},
            'http2_priority': _DEFAULT_PRIORITY,
        }

        push_recipient = RecipientFactory.http2()
        push_recipient.put_event({'type': 'http.request', 'body': b'', 'more_body': False})
        push_sender = SenderFactory.http2(
            self._writer, self.factory, push_stream_id, push_callback=None)
        log_record = _make_log_record(pushed_scope)
        capturing_send = _make_capturing_send(push_sender, log_record)

        if self._task_group is not None:
            if self._aggregator is not None:
                stream_actor = StreamActor(
                    stream_id=push_stream_id,
                    scope=pushed_scope,
                    receive=push_recipient,
                    send=capturing_send,
                    app=self.app,
                    aggregator=self._aggregator,
                    http2_actor=self,
                    log_record=log_record,
                )
                self._task_group.create_task(stream_actor.run())
            else:
                self._legacy_spawn(
                    self._task_group, pushed_scope, push_recipient, capturing_send, log_record)

    async def _handle(self, msg: Message) -> None:  # never reached
        raise NotImplementedError
