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
from ..protocol.frame_types import (
    ErrorCodes, FrameBase, FrameTypes,
    DEFAULT_INITIAL_WINDOW_SIZE, DEFAULT_MAX_FRAME_SIZE,
)
from ..protocol.stream import Stream, StreamState
from .headers import Headers
from .parser import ParserFactory, parse_headers
from .recipient import (AbstractReader, IncompleteReadError,
                        RecipientFactory, _HTTP2_STREAM_QUEUE_DEPTH)
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


def _extract_content_length(scope: dict) -> int | None:
    """Return the int value of the request's content-length, or None.

    Returns None when the header is absent OR when the value does not parse
    as a non-negative integer (parse_headers may have already rejected the
    request as malformed in the latter case).  RFC 9113 §8.1.2.6 covers the
    "must equal sum of DATA payloads" semantics enforced by the caller.
    """
    for name, value in scope.get('headers', []):
        nb = name if isinstance(name, bytes) else bytes(name)
        if nb == b'content-length':
            try:
                n = int(value if isinstance(value, bytes) else bytes(value))
            except (ValueError, TypeError):
                return None
            return n if n >= 0 else None
    return None


def _signal_recipients(recipients: dict[int, _StreamRecipient]) -> None:
    """Inject http.disconnect into every active stream recipient."""
    for recipient in recipients.values():
        recipient.put_disconnect()


def _make_log_record(scope):
    return AccessLogRecord.from_scope(scope)
_access_logger = logging.getLogger('blackbull.access')

_DEFAULT_PRIORITY: dict[str, int | bool] = {'urgency': 3, 'incremental': False}
_HTTP2_EXTENSIONS: dict = {ASGIEvent.HTTP_RESPONSE_PUSH: {}}


async def _run_guarded(coro, sem):
    async with sem:
        await coro


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
        except Exception:
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
        stream_queue_depth: int = _HTTP2_STREAM_QUEUE_DEPTH,
    ) -> None:
        super().__init__()
        self._reader = reader
        self._writer = writer
        self._app = app
        self._aggregator = aggregator
        self._peername = peername
        self._sockname = sockname
        self._ssl = ssl
        self._stream_queue_depth = stream_queue_depth

        self.app = app
        self.reader = reader

        # HTTP/2 connection state
        self.root_stream = Stream(0, None, 1)
        self.factory = FrameFactory()
        self._control_sender = SenderFactory.http2(writer, self.factory, 0)
        self._senders: dict = {}
        # Read from env at construction so tests can override before run().
        from ..env import get_settings as _get_settings  # noqa: PLC0415
        _cfg = _get_settings()
        self.max_concurrent_streams: int = _cfg.h2_max_concurrent_streams
        self._request_timeout: float = _cfg.request_timeout
        self._frame_yield_every: int = _cfg.frame_yield_every
        # Per-connection semaphore: caps concurrently-running stream handlers to
        # prevent a high-mux connection from starving other connections on the
        # same worker.  None means no cap.
        # Single-worker uses BB_H2_ACTIVE_STREAMS_1W (default 20) because one
        # event loop can be overwhelmed by 2,500 tasks at -c50 -m50.
        # Multi-worker uses BB_H2_ACTIVE_STREAMS (default 0 = no limit) because
        # SO_REUSEPORT distributes connections across workers.
        if _cfg.workers == 1:
            _stream_cap = _cfg.h2_active_streams_1w
        else:
            _stream_cap = _cfg.h2_active_streams
        self._stream_semaphore: asyncio.Semaphore | None = (
            asyncio.Semaphore(_stream_cap) if _stream_cap > 0 else None
        )
        self._next_push_stream_id = 2
        self._task_group: asyncio.TaskGroup | None = None

        # Flow-control state — updated by SettingsResponder / WindowUpdateResponder
        # so that new stream senders start with the current peer-granted windows.
        self._peer_initial_window_size: int = DEFAULT_INITIAL_WINDOW_SIZE
        self._connection_window_size: int = DEFAULT_INITIAL_WINDOW_SIZE

        # Concurrent-stream counter — incremented when a stream task is spawned,
        # decremented via Task.add_done_callback when the task finishes.
        self._active_stream_count: int = 0

        # Per-stream recipients, keyed by stream_id.  Stored on the actor so
        # _make_done_cb can remove entries when streams complete.
        self._recipients: dict[int, _StreamRecipient] = {}

        # Set when we have sent a GOAWAY for a connection-level protocol error.
        # The frame loop exits cleanly on the next iteration, giving the GOAWAY
        # time to flush before the connection closes.
        self._goaway_sent: bool = False

        # RFC 9113 §5.1.1 — peer-initiated stream IDs must strictly increase.
        self._last_peer_stream_id: int = 0

        # RFC 9113 §5.1 — closed-stream state, separate from the priority tree.
        # Maps stream_id → True if closed via RST_STREAM, False if closed via
        # END_STREAM / local completion.  This lets us drop closed nodes from
        # root_stream.children (keeping that dict O(active-streams) and so
        # find_child stays O(1)) while still recognizing late frames on
        # already-closed identifiers and choosing the right error code.
        self._closed_streams: dict[int, bool] = {}

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
            sender = SenderFactory.http2(
                self._writer, self.factory, stream_id,
                push_callback=self._handle_push,
            )
            # Initialise with the windows the peer has currently granted us,
            # rather than the RFC defaults, which may already have been exceeded
            # by SETTINGS / WINDOW_UPDATE frames received before this stream opened.
            sender.stream_window_size[stream_id] = self._peer_initial_window_size
            sender.connection_window_size = self._connection_window_size
            self._senders[stream_id] = sender
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

    def _validate_stream_state(
        self, stream: Stream, frame_type: FrameTypes,
    ) -> tuple[ErrorCodes, str] | None:
        """Return (error_code, level) if frame_type is not allowed in stream.state.

        Per RFC 9113 §5.1.  Returns ``None`` when the frame is allowed.
        ``level`` is ``'connection'`` or ``'stream'``.
        """
        state = stream.state

        # IDLE: only HEADERS, PRIORITY, PUSH_PROMISE allowed.  Anything else
        # is a connection PROTOCOL_ERROR (§5.1 #1 and #3 — DATA / WINDOW_UPDATE
        # arriving before HEADERS).
        if state == StreamState.IDLE:
            if frame_type in (FrameTypes.HEADERS, FrameTypes.PRIORITY,
                              FrameTypes.CONTINUATION, FrameTypes.PUSH_PROMISE):
                return None
            return (ErrorCodes.PROTOCOL_ERROR, 'connection')

        # HALF_CLOSED (remote) — the peer has signaled END_STREAM.  Only
        # PRIORITY, WINDOW_UPDATE, and RST_STREAM are permitted from them.
        if state == StreamState.HALF_CLOSED_REMOTE:
            if frame_type in (FrameTypes.PRIORITY, FrameTypes.WINDOW_UPDATE,
                              FrameTypes.RST_STREAM):
                return None
            return (ErrorCodes.STREAM_CLOSED, 'stream')

        # CLOSED — only PRIORITY is unconditionally allowed.  Attempting to
        # send HEADERS or CONTINUATION on a closed stream is a connection
        # error (would otherwise reopen the stream — the peer can't recover
        # by retrying on the same stream id).  DATA / WINDOW_UPDATE on a
        # closed stream are stream errors (the peer may simply be racing
        # against our RST_STREAM / END_STREAM).
        if state == StreamState.CLOSED:
            if frame_type == FrameTypes.PRIORITY:
                return None
            if frame_type in (FrameTypes.HEADERS, FrameTypes.CONTINUATION):
                return (ErrorCodes.STREAM_CLOSED, 'connection')
            return (ErrorCodes.STREAM_CLOSED, 'stream')

        return None

    async def _connection_error(
        self, error_code: 'ErrorCodes', reason: str = '',
    ) -> None:
        """Send GOAWAY with ``error_code``, half-close the writer, mark exit.

        h2spec's VerifyConnectionClose only succeeds on a real TCP close,
        so after flushing the GOAWAY we close our write half (sending FIN)
        and let the frame loop drain on the next iteration via EOF.
        Idempotent — a second call is a no-op.
        """
        if self._goaway_sent:
            return
        logger.warning(
            'HTTP/2 connection error %s: %s', error_code.name, reason)
        await self.send_frame(
            self.factory.goaway(self._last_peer_stream_id, error_code))
        self._goaway_sent = True
        # Close the writer half so the peer sees FIN after the GOAWAY.
        try:
            await self._writer.close()
        except Exception:
            # The writer may already be closed (e.g. peer hung up).  We
            # have done our part; let the frame loop drain.
            logger.debug('writer.close raised on connection-error path',
                         exc_info=True)

    def _make_done_cb(self, stream_id: int) -> Callable[[asyncio.Task], None]:
        """Return a done-callback that releases per-stream resources on completion.

        Keeps the Stream node in the tree (marked CLOSED) so that the
        frame-loop state validation can detect late frames arriving on the
        same identifier and respond with the appropriate STREAM_CLOSED
        error (RFC 9113 §5.1).
        """
        def _cb(_task: asyncio.Task) -> None:
            self._active_stream_count = max(0, self._active_stream_count - 1)
            self._senders.pop(stream_id, None)
            self._recipients.pop(stream_id, None)
            # Prune the stream node from the tree and remember it as closed-
            # via-END_STREAM (closed_via_rst=False) so late frames hit the
            # CLOSED branch of §5.1 validation without keeping a Stream
            # object around for every completed request.
            if self.root_stream.children.pop(stream_id, None) is not None:
                self._closed_streams[stream_id] = False
        return _cb

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
        from ..env import get_settings as _get_settings  # noqa: PLC0415
        cfg = _get_settings()

        await self.send_frame(self.factory.settings(
            enable_connect_protocol=True,
            initial_window_size=cfg.h2_initial_window_size,
            max_concurrent_streams=self.max_concurrent_streams,
        ))
        logger.info(
            'HTTP/2 SETTINGS sent: initial_window_size=%d max_concurrent_streams=%d',
            cfg.h2_initial_window_size, self.max_concurrent_streams,
        )

        # Expand the connection-level inbound window beyond the RFC default of 65535.
        conn_increment = cfg.h2_connection_window_size - DEFAULT_INITIAL_WINDOW_SIZE
        if conn_increment > 0:
            await self.send_frame(self.factory.window_update(0, conn_increment))

        self._recipients.clear()

        async with asyncio.TaskGroup() as tg:
            self._task_group = tg
            await self._frame_loop(tg)

        self._task_group = None

    async def _frame_loop(self, tg: asyncio.TaskGroup) -> None:
        """Read frames and dispatch stream tasks until EOF or GOAWAY."""
        waiting_continuation = False
        header_frame = None
        _tasks_since_yield = 0
        _yield_every = self._frame_yield_every

        while data := await self.receive():
            if self._goaway_sent:
                # A previous frame triggered a connection error and the GOAWAY
                # has been flushed.  Exit cleanly so the writer can close.
                return
            frame = self.factory.load(data)
            frame_type = frame.FrameType()

            # RFC 9113 §6.10 — while awaiting CONTINUATION after a HEADERS or
            # PUSH_PROMISE without END_HEADERS, any frame other than a matching
            # CONTINUATION (including unknown frame types) is a connection
            # error of type PROTOCOL_ERROR.
            if waiting_continuation and frame_type != FrameTypes.CONTINUATION:
                name = frame_type.name if frame_type is not None else 'unknown'
                await self._connection_error(
                    ErrorCodes.PROTOCOL_ERROR,
                    f'expected CONTINUATION, got {name}')
                continue  # let h2spec read the GOAWAY before we close

            # RFC 9113 §5.5 — outside a header block, frames of unknown type
            # are silently ignored.
            if frame_type is None:
                continue

            # RFC 9113 §4.2 — a frame whose payload exceeds the receiver's
            # advertised SETTINGS_MAX_FRAME_SIZE is a FRAME_SIZE_ERROR.  When
            # the frame could alter connection state (carries a header block,
            # is SETTINGS, or targets stream 0), it is a connection error;
            # otherwise it is a stream error.
            if frame.length > DEFAULT_MAX_FRAME_SIZE:
                _state_altering = frame_type in (
                    FrameTypes.HEADERS, FrameTypes.CONTINUATION,
                    FrameTypes.PUSH_PROMISE, FrameTypes.SETTINGS,
                )
                if _state_altering or frame.stream_id == 0:
                    await self._connection_error(
                        ErrorCodes.FRAME_SIZE_ERROR,
                        f'{frame_type.name} length {frame.length} > '
                        f'{DEFAULT_MAX_FRAME_SIZE}')
                else:
                    await self.send_frame(self.factory.rst_stream(
                        frame.stream_id, ErrorCodes.FRAME_SIZE_ERROR))
                continue

            # RFC 9113 §6.2 — HEADERS MUST be associated with a non-zero
            # stream identifier (PROTOCOL_ERROR at the connection level).
            if frame_type == FrameTypes.HEADERS and frame.stream_id == 0:
                await self._connection_error(
                    ErrorCodes.PROTOCOL_ERROR,
                    'HEADERS with stream_id 0')
                continue

            # RFC 9113 §6.10 — CONTINUATION MUST be associated with a non-zero
            # stream identifier (PROTOCOL_ERROR at the connection level).
            if frame_type == FrameTypes.CONTINUATION and frame.stream_id == 0:
                await self._connection_error(
                    ErrorCodes.PROTOCOL_ERROR,
                    'CONTINUATION with stream_id 0')
                continue

            # RFC 9113 §6.10 — CONTINUATION outside an open header block is a
            # connection PROTOCOL_ERROR, independent of stream state.  This
            # check must precede stream-state validation, otherwise a stray
            # CONTINUATION on a half-closed/closed stream would be rejected
            # with the wrong error type (STREAM_CLOSED / RST_STREAM).
            if frame_type == FrameTypes.CONTINUATION and not waiting_continuation:
                await self._connection_error(
                    ErrorCodes.PROTOCOL_ERROR,
                    'unexpected CONTINUATION without preceding HEADERS')
                continue

            # RFC 9113 §6.1 — DATA MUST be associated with a non-zero stream
            # identifier (PROTOCOL_ERROR at the connection level).
            if frame_type == FrameTypes.DATA and frame.stream_id == 0:
                await self._connection_error(
                    ErrorCodes.PROTOCOL_ERROR,
                    'DATA with stream_id 0')
                continue

            # RFC 9113 §6.3 — PRIORITY MUST be associated with a non-zero
            # stream identifier (PROTOCOL_ERROR at the connection level).
            if frame_type == FrameTypes.PRIORITY and frame.stream_id == 0:
                await self._connection_error(
                    ErrorCodes.PROTOCOL_ERROR,
                    'PRIORITY with stream_id 0')
                continue

            # RFC 9113 §6.3 — PRIORITY frame payload MUST be 5 octets;
            # otherwise it is a stream error of type FRAME_SIZE_ERROR.
            if frame_type == FrameTypes.PRIORITY and frame.length != 5:
                await self.send_frame(self.factory.rst_stream(
                    frame.stream_id, ErrorCodes.FRAME_SIZE_ERROR))
                continue

            # Fast lookup: live streams live directly under root.  Closed
            # streams have already been pruned from the tree; their identifier
            # lives in self._closed_streams so we can still distinguish CLOSED
            # from IDLE for §5.1 validation.
            stream = self.root_stream.children.get(frame.stream_id)

            if frame.stream_id != 0 and stream is None:
                if frame.stream_id in self._closed_streams:
                    # Late frame on a CLOSED stream (§5.1).
                    if frame_type == FrameTypes.PRIORITY:
                        pass  # PRIORITY is always allowed; let it through
                    elif frame_type in (FrameTypes.HEADERS, FrameTypes.CONTINUATION):
                        await self._connection_error(
                            ErrorCodes.STREAM_CLOSED,
                            f'{frame_type.name} on closed stream {frame.stream_id}')
                        continue
                    else:
                        await self.send_frame(self.factory.rst_stream(
                            frame.stream_id, ErrorCodes.STREAM_CLOSED))
                        continue
                else:
                    # Stream not seen before — must be a stream-creating frame
                    # (HEADERS or PRIORITY).  Anything else is IDLE-state error.
                    if frame_type == FrameTypes.HEADERS:
                        # RFC 9113 §5.1.1 — peer streams MUST use odd
                        # identifiers strictly greater than every previous
                        # peer-initiated stream.
                        if frame.stream_id % 2 == 0:
                            await self._connection_error(
                                ErrorCodes.PROTOCOL_ERROR,
                                f'peer used even stream_id={frame.stream_id}')
                            continue
                        if frame.stream_id <= self._last_peer_stream_id:
                            await self._connection_error(
                                ErrorCodes.PROTOCOL_ERROR,
                                f'peer stream_id={frame.stream_id} '
                                f'<= last={self._last_peer_stream_id}')
                            continue
                        self._last_peer_stream_id = frame.stream_id
                        stream = self.root_stream.add_child(frame.stream_id)
                    elif frame_type == FrameTypes.PRIORITY:
                        stream = self.root_stream.add_child(frame.stream_id)
                    else:
                        await self._connection_error(
                            ErrorCodes.PROTOCOL_ERROR,
                            f'frame {frame_type.name} on idle stream '
                            f'{frame.stream_id}')
                        continue

            # RFC 9113 §5.1 — validate the frame against the stream state.
            # Skip stream-id 0 (connection-level frames don't have stream state).
            if stream is not None and frame.stream_id != 0:
                err = self._validate_stream_state(stream, frame_type)
                if err is not None:
                    error_code, level = err
                    if level == 'connection':
                        await self._connection_error(
                            error_code,
                            f'frame {frame_type.name} on stream {frame.stream_id} '
                            f'in {stream.state.name} state')
                        continue
                    # stream-level error
                    await self.send_frame(
                        self.factory.rst_stream(frame.stream_id, error_code))
                    continue

            spawned = False
            match frame.FrameType():
                case FrameTypes.HEADERS:
                    send = self.make_sender(stream.stream_id)
                    spawned = await self._on_headers_frame(frame, stream, send, tg)
                    if not spawned:
                        waiting_continuation = True
                        header_frame = frame
                case FrameTypes.CONTINUATION:
                    send = self.make_sender(stream.stream_id)
                    spawned = await self._on_continuation_frame(
                        frame, stream, send, tg, header_frame, waiting_continuation)
                    if spawned:
                        waiting_continuation = False
                case FrameTypes.DATA:
                    await self._on_data_frame(frame, stream)
                case FrameTypes.GOAWAY:
                    await self._on_goaway_frame(self._last_peer_stream_id)
                    return
                case _:
                    await ResponderFactory.create(frame).respond(self)

            if spawned and _yield_every > 0:
                _tasks_since_yield += 1
                if _tasks_since_yield >= _yield_every:
                    await asyncio.sleep(0)
                    _tasks_since_yield = 0

        _signal_recipients(self._recipients)

    def _spawn_stream_task(
        self,
        tg: asyncio.TaskGroup,
        stream_id: int,
        scope: dict,
        recipient,
        send,
        log_record,
    ) -> None:
        """Spawn a StreamActor (aggregator path) or legacy _run_with_log task.

        Increments ``_active_stream_count`` and registers ``_on_stream_done``
        so the counter is decremented when the task finishes.

        When ``BB_REQUEST_TIMEOUT > 0``, the stream coroutine is wrapped with
        ``asyncio.wait_for``; on expiry RST_STREAM CANCEL is sent and the task
        completes normally (does not cancel the TaskGroup).
        """
        self._active_stream_count += 1

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
            coro = stream_actor.run()
        else:
            from .server import _run_with_log  # noqa: PLC0415
            dispatcher = getattr(self.app, '_dispatcher', None)
            coro = _run_with_log(
                self.app(scope, recipient, send),
                log_record,
                dispatcher=dispatcher,
                scope=scope,
            )

        timeout = self._request_timeout
        if timeout > 0:
            async def _timed(c=coro, sid=stream_id, t=timeout):
                try:
                    await asyncio.wait_for(c, timeout=t)
                except asyncio.TimeoutError:
                    logger.warning(
                        'Stream %d timed out after %.1fs — RST_STREAM CANCEL', sid, t)
                    await self.send_frame(self.factory.rst_stream(sid, ErrorCodes.CANCEL))
            final_coro = _timed()
        else:
            final_coro = coro

        if self._stream_semaphore is not None:
            task = tg.create_task(_run_guarded(final_coro, self._stream_semaphore))
        else:
            task = tg.create_task(final_coro)

        task.add_done_callback(self._make_done_cb(stream_id))

    async def _on_headers_frame(
        self,
        frame,
        stream: 'Stream',
        send,
        tg: asyncio.TaskGroup,
    ) -> bool:
        """Handle a HEADERS frame; return True if stream task spawned, False if awaiting CONTINUATION."""
        if self._active_stream_count >= self.max_concurrent_streams:
            await self.send_frame(
                self.factory.rst_stream(stream.stream_id, ErrorCodes.REFUSED_STREAM))
            return True  # refused — do not queue as waiting for CONTINUATION

        if not frame.end_headers:
            return False

        scope = parse_headers(frame)
        # RFC 9113 §8.1.2 / §8.2 — malformed HEADERS must be rejected with
        # a stream error of type PROTOCOL_ERROR rather than dispatched.
        # parse_payload sets the flag for field-level violations; parse_headers
        # sets it for missing/empty required pseudo-headers.
        if getattr(frame, 'malformed', False):
            logger.debug('Stream %d malformed HEADERS — %s',
                         stream.stream_id, frame.malformed_reason)
            await self.send_frame(
                self.factory.rst_stream(stream.stream_id, ErrorCodes.PROTOCOL_ERROR))
            return True

        stream.expected_content_length = _extract_content_length(scope)
        self._fill_scope_connection(scope)
        stream.scope = scope

        if scope.get('type') == 'websocket':
            # RFC 8441 — Extended CONNECT bootstrapping WebSocket over HTTP/2
            stream.on_headers_received(end_stream=False)
            log_record = _make_log_record(scope)
            await self._handle_h2_websocket(stream, tg, log_record)
            return True

        scope['http2_priority'] = _resolve_priority(stream, scope)
        scope['extensions'] = _HTTP2_EXTENSIONS
        stream_recipient = RecipientFactory.http2(queue_depth=self._stream_queue_depth)
        self._recipients[stream.stream_id] = stream_recipient
        stream.on_headers_received(end_stream=bool(frame.end_stream))
        if frame.end_stream:
            # No body to deliver — skip queue allocation; recipient synthesizes
            # the empty http.request event on first receive() call if needed.
            stream_recipient.mark_end_of_stream_on_headers()
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
        header_frame,
        waiting_continuation: bool,
    ) -> bool:
        """Handle a CONTINUATION frame; return True if stream task spawned, False if still accumulating."""
        # RFC 9113 §6.10 — CONTINUATION outside an unterminated header block
        # (i.e. not preceded by HEADERS/CONTINUATION without END_HEADERS) is a
        # connection error of type PROTOCOL_ERROR.
        if not waiting_continuation or header_frame is None:
            await self._connection_error(
                ErrorCodes.PROTOCOL_ERROR,
                'unexpected CONTINUATION without preceding HEADERS')
            return True

        header_frame.raw_block += frame.payload

        if not frame.end_headers:
            return False

        header_frame.parse_payload()

        scope = parse_headers(header_frame)
        # RFC 9113 §8.1.2 / §8.2 — same malformed-HEADERS check as the direct
        # HEADERS path; reject with RST_STREAM PROTOCOL_ERROR.
        if getattr(header_frame, 'malformed', False):
            logger.debug('Stream %d malformed HEADERS (via CONTINUATION) — %s',
                         stream.stream_id, header_frame.malformed_reason)
            await self.send_frame(
                self.factory.rst_stream(stream.stream_id, ErrorCodes.PROTOCOL_ERROR))
            return True

        stream.expected_content_length = _extract_content_length(scope)
        self._fill_scope_connection(scope)

        if self._active_stream_count >= self.max_concurrent_streams:
            await self.send_frame(
                self.factory.rst_stream(stream.stream_id, ErrorCodes.REFUSED_STREAM))
            return True

        scope['http2_priority'] = _resolve_priority(stream, scope)
        scope['extensions'] = _HTTP2_EXTENSIONS
        stream.scope = scope
        stream_recipient = RecipientFactory.http2(queue_depth=self._stream_queue_depth)
        self._recipients[stream.stream_id] = stream_recipient
        log_record = _make_log_record(scope)
        capturing_send = _make_capturing_send(send, log_record)
        self._spawn_stream_task(tg, stream.stream_id, scope, stream_recipient, capturing_send, log_record)
        return True

    async def _on_data_frame(self, frame, stream: 'Stream') -> None:
        """Handle a DATA frame: deliver to the stream's recipient then issue WINDOW_UPDATE.

        WINDOW_UPDATE is sent only after successful delivery so that a full
        recipient queue withholds flow-control credit instead of silently
        accepting data the app cannot process (RFC 7540 §6.9).
        """
        if stream.state in (StreamState.HALF_CLOSED_REMOTE, StreamState.CLOSED):
            await self.send_frame(
                self.factory.rst_stream(stream.stream_id, ErrorCodes.STREAM_CLOSED))
            return

        # RFC 9113 §8.1.2.6 — validate the sum of DATA payload lengths
        # against the declared content-length.  Excess is detected on each
        # frame; deficit is detected when END_STREAM arrives.  Padding bytes
        # are not counted in the payload length per §8.1.2.6.
        stream.received_data_bytes += len(frame.payload)
        expected = stream.expected_content_length
        if expected is not None:
            if stream.received_data_bytes > expected or (
                frame.end_stream and stream.received_data_bytes != expected
            ):
                await self.send_frame(self.factory.rst_stream(
                    stream.stream_id, ErrorCodes.PROTOCOL_ERROR))
                return

        stream.on_data_received(end_stream=bool(frame.end_stream))
        if stream.stream_id in self._recipients:
            delivered = self._recipients[stream.stream_id].put_DATAFrame(frame)
            if delivered:
                await self.send_frame(
                    self.factory.window_update(stream.stream_id, frame.length))
            else:
                await self.send_frame(
                    self.factory.rst_stream(stream.stream_id, ErrorCodes.ENHANCE_YOUR_CALM))
        else:
            logger.warning('DATA for stream %d but no recipient found', stream.stream_id)

    async def _on_goaway_frame(self, last_stream_id: int) -> None:
        """Handle an incoming GOAWAY: echo one back and signal all recipients."""
        await self.send_frame(self.factory.goaway(last_stream_id))
        _signal_recipients(self._recipients)

    async def _handle_h2_websocket(
        self,
        stream: 'Stream',
        tg: asyncio.TaskGroup,
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
        self._recipients[stream.stream_id] = ws_reader  # DATA frames routed here

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

        self._active_stream_count += 1
        task = tg.create_task(_run_ws())
        task.add_done_callback(self._make_done_cb(stream.stream_id))

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
            'extensions': _HTTP2_EXTENSIONS,
            'http2_priority': _DEFAULT_PRIORITY,
        }

        push_recipient = RecipientFactory.http2(queue_depth=self._stream_queue_depth)
        # Pushed requests have no body — same lazy-queue path as GETs with END_STREAM on HEADERS.
        push_recipient.mark_end_of_stream_on_headers()
        self._recipients[push_stream_id] = push_recipient
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
