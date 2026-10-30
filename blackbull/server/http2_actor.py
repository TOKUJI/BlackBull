"""HTTP/2 Actor classes for the BlackBull actor model.

HTTP2Actor drives the HTTP/2 connection state machine for one TCP connection.
StreamActor owns the lifetime of a single HTTP/2 stream.
"""
import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from http import HTTPStatus
from typing import Protocol, runtime_checkable

from hpack import OversizedHeaderListError

from ..actor import Actor, Message
from ..env import get_settings
from ..event_aggregator import EventAggregator
from ..logger import log, debug_gate
from ..protocol.frame import FrameFactory
from ..protocol.field_grammar import PROHIBITED_TRAILER_FIELDS
from ..protocol.framing import method_is, parse_content_length
from ..protocol.frame_types import (
    ErrorCodes, FrameBase, FrameTypes,
    DEFAULT_INITIAL_WINDOW_SIZE, DEFAULT_MAX_FRAME_SIZE,
)
from ..protocol.stream import Stream, StreamState
from ..connection import Connection
from ..headers import Headers
from .parser import parse_headers
from .deadline import Deadline
from .cap_log import log_cap_hit
from .recipient import (AbstractReader, IncompleteReadError,
                        HTTP2Recipient, RecipientFactory,
                        _HTTP2_STREAM_QUEUE_DEPTH)
from .http2_connection import SKIP, HTTP2Connection
from .sender import AbstractWriter, FlowControlStalled, SenderFactory
from .access_log import (
    close_record as _close_record,
    close_ws_record as _close_ws_record,
    open_record as _open_record,
    start_record as _start_record,
)
from ..asgi import ASGIEvent, ASGIReceiveCallable, ASGISendCallable
from ..native import NativeResponse
from .http1_actor import RequestActor

logger = logging.getLogger(__name__)
# DEBUG enablement is fixed at import.
_DEBUG = debug_gate(logger)



@runtime_checkable
class _StreamRecipient(Protocol):
    """Duck-type interface shared by HTTP2Recipient and HTTP2StreamReader."""
    def put_disconnect(self) -> None: pass
    def put_DATAFrame(self, frame) -> bool: pass


def _req_headers(conn: Connection) -> Headers:
    """The one source both header readers share, so they cannot drift apart."""
    return conn.headers


def _signal_recipients(recipients: dict[int, _StreamRecipient]) -> None:
    """Inject http.disconnect into every active stream recipient."""
    for recipient in recipients.values():
        recipient.put_disconnect()


_DEFAULT_PRIORITY: dict[str, int | bool] = {'urgency': 3, 'incremental': False}


def _build_h2_extensions(
    stream_id: int,
    priority: dict,
    peer_initial_window: int,
    connection_window: int,
    peer_push_permitted: bool = True,
) -> dict:
    """Return a freshly-built ``scope['extensions']`` dict for one HTTP/2 request.

    Priority field *names* match the gunicorn beta HTTP/2 surface; the values
    are RFC 9218 §4.1 (see ``docs/about/rfc9113-implementation.md`` §5.3).
    The window numbers are a snapshot taken here and go stale as the body
    streams.  Peer recv-window is deliberately absent: credit is replayed per
    consumed DATA frame, so there is no scalar to snapshot.
    """
    extensions = {
        'http.response.priority': priority,
        'http.response.http2_stream': {
            'stream_id': stream_id,
            'send_window_remaining': peer_initial_window,
            'connection_send_window_remaining': connection_window,
        },
    }
    if peer_push_permitted:
        extensions[ASGIEvent.HTTP_RESPONSE_PUSH] = {}
    return extensions




def _schedule_group_task(
    tg: asyncio.TaskGroup, coro,
) -> asyncio.Task | None:
    """Schedule *coro* on *tg*, closing it when the group refuses it.

    A group that is shutting down raises from ``create_task`` before it wraps
    the coroutine, and 3.11/3.12 leave that coroutine open for the GC to
    report as never awaited — 3.13 closes it — so it is closed here either
    way.  ``None`` means the group would not take it.
    """
    try:
        return tg.create_task(coro)
    except RuntimeError:
        coro.close()
        return None


def _schedule_loop_task(coro) -> asyncio.Task | None:
    """Schedule *coro* on the running loop, closing it if the loop refuses."""
    try:
        return asyncio.get_running_loop().create_task(coro)
    except RuntimeError:
        coro.close()
        return None


async def _run_when_stream_cap_admits(start_stream, cap):
    """Run *start_stream()* once *cap* admits it.

    A factory rather than a coroutine: a task cancelled while still waiting
    never reaches the body, and a coroutine built before that is destroyed
    un-awaited.
    """
    async with cap:
        await start_stream()


def _resolve_priority(stream: 'Stream', conn: Connection) -> dict[str, int | bool]:
    if stream.priority_hint is not None:
        return stream.priority_hint
    raw = _req_headers(conn).get(b'priority', b'')
    if raw:
        from ..protocol.frame_types import parse_priority_field
        return parse_priority_field(raw.decode('ascii', errors='replace'))
    return dict(_DEFAULT_PRIORITY)


class StreamActor(Actor):
    """Run one HTTP/2 request through RequestActor with a native Connection.

    Isolate unhandled failure with RST_STREAM; do not share per-stream state.
    """

    def __init__(
        self,
        stream_id: int,
        conn: Connection,
        receive: ASGIReceiveCallable,
        send: ASGISendCallable,
        app: Callable[..., Awaitable[None]],
        # ``None`` for a foreign ASGI app: the actor skips event emission
        # rather than the caller forking to a second dispatch path.
        aggregator: EventAggregator | None,
        http2_actor: 'HTTP2Actor',
        log_record,
        force_asgi: bool,
    ) -> None:
        super().__init__()
        self._stream_id = stream_id
        self._conn = conn
        self._receive = receive
        self._send = send
        self._app = app
        self._aggregator = aggregator
        self._http2_actor = http2_actor
        self._log_record = log_record
        self._force_asgi = force_asgi

    async def run(self) -> None:
        try:
            await RequestActor(
                self._conn, self._receive, self._send,
                self._app, self._aggregator, self._force_asgi,
            ).run()
        except FlowControlStalled:
            # CANCEL, not INTERNAL_ERROR — see rfc9113-implementation.md §10.5,
            # "Data dribble".
            await self._http2_actor.send_frame(
                self._http2_actor.factory.rst_stream(
                    self._stream_id, ErrorCodes.CANCEL)
            )
        except Exception:
            if not self._send._poisoned:    # the sender already reset it
                await self._http2_actor.send_frame(
                    self._http2_actor.factory.rst_stream(
                        self._stream_id, ErrorCodes.INTERNAL_ERROR)
                )
        finally:
            # ``None`` on the baseline hot path; ``_close_record`` tolerates it.
            _close_record(self._log_record)

    async def _handle(self, msg: Message) -> None:
        raise NotImplementedError


class HTTP2Actor(Actor, HTTP2Connection):
    """Drives the HTTP/2 connection state machine for one connection.

    Supervisor strategy: propagate — framing errors send GOAWAY and raise,
    surfacing to the caller.

    If *aggregator* is ``None`` the actor dispatches events directly through
    ``app._dispatcher`` instead.
    """

    def __init__(
        self,
        reader: 'AbstractReader | None',
        writer: AbstractWriter,
        app: Callable[..., Awaitable[None]],
        aggregator: 'EventAggregator | None',
        *,
        peername: tuple[str, int | None] | None = None,
        sockname: tuple[str, int | None] | None = None,
        ssl: bool = False,
        stream_queue_depth: int = _HTTP2_STREAM_QUEUE_DEPTH,
        connection_id: str = '',
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
        # ConnectionActor's accept-time id: one id for the whole connection,
        # reused by the RFC 8441 WS path.  Empty when the actor is built
        # directly (tests), where that path mints a fresh one.
        self._connection_id = connection_id

        self.app = app
        self.reader = reader

        # Read from env at construction so tests can override before run().
        _cfg = get_settings()
        header_list_size = min(_cfg.header_max_total, 0xFFFFFFFF) or 0xFFFFFFFF
        self._init_connection(
            FrameFactory(max_header_list_size=header_list_size),
            field_block_max=_cfg.header_max_total, field_block_cap='header_max_total')
        self._control_sender = SenderFactory.http2(writer, self.factory, 0)
        self.max_concurrent_streams: int = _cfg.h2_max_concurrent_streams
        self._request_timeout: float = _cfg.request_timeout
        self._frame_yield_every: int = _cfg.frame_yield_every
        self._max_body_size: int = _cfg.max_body_size
        # These limits are connection-scoped on purpose: every one is
        # process-wide configuration, and a stream is a request — reading them
        # per stream would put a settings lookup on the per-request path.
        self._min_body_rate: float = _cfg.min_body_rate
        self._min_body_rate_grace: float = _cfg.min_body_rate_grace
        self._write_timeout: float = _cfg.write_timeout
        # SO_REUSEPORT spreads them over several, hence the second setting.
        all_streams_on_one_loop = _cfg.workers == 1
        if all_streams_on_one_loop:
            _stream_cap = _cfg.h2_active_streams_1w
        else:
            _stream_cap = _cfg.h2_active_streams
        self._stream_semaphore: asyncio.Semaphore | None = (
            asyncio.Semaphore(_stream_cap) if _stream_cap > 0 else None
        )
        self._next_push_stream_id = 2
        self._task_group: asyncio.TaskGroup | None = None

        # Enforced by ``_check_liveness``, which closes the writer: a Guard
        # would cancel the frame loop.
        self._h2_idle_timeout: float = _cfg.h2_idle_timeout
        self._h2_ping_timeout: float = _cfg.h2_ping_timeout
        self._header_timeout: float = _cfg.header_timeout
        self._last_frame_at: float = 0.0
        self._probe_sent_at: float | None = None

        # Also each recipient's byte budget: a conformant peer can never hold
        # more un-credited bytes in flight than the window we advertised.
        self._inbound_stream_window: int = _cfg.h2_initial_window_size
        # Held so the loop's weak refs can't drop a pending credit replay.
        self._credit_flush_tasks: set[asyncio.Task] = set()

        self._active_stream_count: int = 0

        # Kept so an inbound RST_STREAM can cancel the running handler.
        # Otherwise a server-streaming handler abandoned mid-flight blocks
        # forever in the sender's flow-control wait — the departed client never
        # sends WINDOW_UPDATE — holding a max_concurrent_streams slot until a
        # high-churn client has leaked every one of them.
        self._stream_tasks: dict[int, asyncio.Task] = {}

        self._recipients: dict[int, _StreamRecipient] = {}

        # RFC 8441; run() sets both of these from the environment.  The
        # defaults here are for tests that drive the frame handlers directly.
        self._ws_over_h2_enabled: bool = False
        self._force_asgi: bool = False

        # RFC 8441 stream-exhaustion guard, capped at
        # ``cfg.h2_ws_max_streams_per_connection``.  The id set makes common
        # retirement decrement the count exactly once.
        self._ws_stream_count: int = 0
        self._ws_stream_ids: set[int] = set()

        # Non-zero when receive() declined to buffer an oversize payload.
        self._oversize_frame_len: int = 0

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

    def make_sender(self, stream_id: int, head_mode: bool | None = None):
        if stream_id not in self._senders:
            sender = SenderFactory.http2(
                self._writer, self.factory, stream_id,
                push_callback=self._handle_push,
                conn_window=self._conn_window,
                # What the peer has currently granted, not the RFC default:
                # SETTINGS received before this stream opened may have moved it.
                initial_window=self._peer_initial_window_size,
                flow_control_timeout=self._write_timeout,
                head_mode=bool(head_mode),
            )
            self._senders[stream_id] = sender
        elif head_mode is not None:
            # Cached before the request head said what the method was.
            self._senders[stream_id]._head_mode = head_mode
        return self._senders[stream_id]

    def _make_stream_recipient(self, stream_id: int) -> HTTP2Recipient:
        """Recipient with consume-time WINDOW_UPDATE crediting.

        Why credit on consumption rather than on delivery:
        ``docs/about/rfc9113-implementation.md`` §6.9.1.
        """

        return RecipientFactory.http2(
            queue_depth=self._stream_queue_depth,
            credit_callback=self._make_consume_credit_callback(stream_id),
            credit_budget=self._inbound_stream_window,
            max_body=self._max_body_size,
            min_rate=self._min_body_rate,
            min_rate_grace=self._min_body_rate_grace,
        )

    def _make_consume_credit_callback(
        self, stream_id: int,
    ) -> Callable[[int], Awaitable[None]]:
        """Return a callback that replays both levels of consumed credit."""

        # Unannotated for the per-request-closure reason (see app.py::_wrap_send_native).
        async def _credit(n):
            task = self._schedule_credit_replay(n)
            if task is None:
                return
            # Consumer cancellation must not revoke connection credit that
            # was earned when the DATA left the recipient queue.
            await asyncio.shield(task)
            if self._goaway_sent or stream_id not in self._recipients:
                return
            try:
                # Stream credit remains stream-owned so RST cancellation can
                # stop a writer suspended after the liveness check.
                await self.send_frame(
                    self.factory.window_update(stream_id, n))
            except Exception:
                if _DEBUG:
                    logger.debug('stream credit replay failed', exc_info=True)

        return _credit

    def _schedule_credit_replay(self, balance: int) -> asyncio.Task | None:
        """Schedule stream-0 WINDOW_UPDATE replay owned by the connection."""
        if balance <= 0 or self._goaway_sent:
            return None

        # Unannotated for the per-request-closure reason (see app.py::_wrap_send_native).
        async def _replay():
            if self._goaway_sent:
                return
            try:
                await self.send_frame(self.factory.window_update(0, balance))
            except Exception:
                if _DEBUG:
                    logger.debug('connection credit replay failed', exc_info=True)

        # A done-callback and stream teardown cannot await this replay.  Keep
        # a strong reference until completion and join the connection's task
        # group whenever it can accept work.
        task = None
        if self._task_group is not None:
            task = _schedule_group_task(self._task_group, _replay())
        if task is None:
            task = _schedule_loop_task(_replay())
        if task is None:
            return None
        self._credit_flush_tasks.add(task)
        task.add_done_callback(self._credit_flush_tasks.discard)
        return task

    def _release_recipient_credit(self, recipient, additional: int = 0) -> None:
        """Replay a released stream's un-consumed inbound credit to stream 0.

        Stream-level credit is not replayed: the stream is closed (RFC 9113
        §5.1).  The connection-level half, and what leaks without it, is
        ``docs/about/rfc9113-implementation.md`` §6.9.1.  ``additional`` is
        the complete current DATA frame, including padding, when refusal
        happens before it enters the recipient.
        """
        take = getattr(recipient, 'take_uncredited', None)
        balance = (take() if take is not None else 0) + additional
        self._schedule_credit_replay(balance)

    def _fill_scope_connection(self, conn: Connection) -> None:
        """Inject peername/sockname into a freshly-parsed HTTP/2 Connection."""
        if self._peername:
            conn.client = tuple(self._peername[:2])
        if self._sockname:
            conn.server = tuple(self._sockname[:2])

    @log
    async def send_frame(self, frame: FrameBase) -> None:
        """Send a control frame and meter every emitted RST_STREAM at this boundary.
        """
        terminal_reset = (frame.FrameType() == FrameTypes.RST_STREAM
                          and not self._goaway_sent)
        if terminal_reset:
            # Stop stream-owned work at the decision boundary, before a
            # transport drain can yield to a producer forbidden from emitting
            # output.  Cleanup must also survive a failed RST write; the
            # connection cannot make that stream live again.
            self._retire_stream(frame.stream_id, via_rst=True)
        await self._control_sender(frame)
        if terminal_reset:
            await self._count_emitted_rst()

    # --- connection-core hooks ------------------------------------------

    async def _send_control(self, frame: FrameBase) -> None:
        await self.send_frame(frame)

    async def _reset(self, stream_id: int, code: ErrorCodes, *,
                     additional_credit: int = 0) -> None:
        if additional_credit:
            self._retire_stream(stream_id, via_rst=True,
                                additional_credit=additional_credit)
        await self.send_frame(self.factory.rst_stream(stream_id, code))

    def _retire_all(self) -> None:
        self._retire_all_streams()

    async def _after_goaway(self, reason: str) -> None:
        try:
            await self._writer.close()
        except Exception:
            # Already closed (peer hung up); we have done our part.
            if _DEBUG:
                logger.debug('writer.close raised on connection-error path',
                             exc_info=True)

    def _peer_reset(self, stream_id: int, error_code) -> None:
        logger.warning('stream_id=%d %s', stream_id, error_code)
        # Retire synchronously; cancellation still runs handler finally/aclose.
        self._retire_stream(stream_id, via_rst=True, by_peer=True)

    async def _open_peer_stream(self, frame, stream: 'Stream | None' = None):
        """§5.1.1: a peer stream id is odd and strictly increasing."""
        stream_id = frame.stream_id
        if stream_id % 2 == 0:
            await self._connection_error(ErrorCodes.PROTOCOL_ERROR,
                                         f'peer used even stream_id={stream_id}')
            return SKIP
        if stream_id <= self._last_peer_stream_id:
            await self._connection_error(
                ErrorCodes.PROTOCOL_ERROR,
                f'peer stream_id={stream_id} <= last={self._last_peer_stream_id}')
            return SKIP
        self._last_peer_stream_id = stream_id
        if stream is None:
            stream = self.root_stream.add_child(stream_id)
        # OPEN until the complete block can apply END_STREAM, so a refused
        # request stays distinct from a node a priority hint reserved.
        stream.state = StreamState.OPEN
        return stream

    def _stream_sender(self, stream_id: int):
        return self.make_sender(stream_id)

    async def _return_connection_credit(self, n: int) -> None:
        self._release_recipient_credit(None, n)

    def _on_block_opened(self) -> None:
        # The liveness deadline was chosen before the block opened.
        self._check_liveness()

    def _field_block_refused(self, exc: Exception) -> ErrorCodes | None:
        if not isinstance(exc, OversizedHeaderListError):
            return None
        limit = self.factory.decoder.max_header_list_size
        # HPACK reports no exact total after aborting; this is a lower bound.
        log_cap_hit('header_max_total', requested=limit + 1, limit=limit,
                    protocol='http2')
        return ErrorCodes.ENHANCE_YOUR_CALM

    def _make_done_cb(
        self, stream_id: int, *, is_ws: bool = False,
    ) -> Callable[[asyncio.Task], None]:
        """Return a done-callback that releases per-stream resources on completion.

        Drops every live owner and retains only the bounded closed-id record.
        ``is_ws`` is tagged at the call site rather than blanket-decremented:
        letting regular HTTP completions touch ``_ws_stream_count`` would
        drift it below the true in-flight count and the RFC 8441 cap would
        over-admit.
        """

        # Unannotated for the per-request-closure reason (see app.py::_wrap_send_native).
        def _cb(_task):
            self._retire_stream(
                stream_id, via_rst=False, finished_task=_task,
                is_ws_hint=is_ws)
        return _cb

    def _retire_stream(
        self,
        stream_id: int,
        *,
        via_rst: bool,
        finished_task: asyncio.Task | None = None,
        additional_credit: int = 0,
        is_ws_hint: bool = False,
        by_peer: bool = False,
    ) -> None:
        """Release every owner of one stream exactly once.

        This is synchronous so task callbacks and frame responders share the
        same transition.  Connection-credit replay remains scheduled because
        a done-callback cannot await it and that write outlives the stream.
        """
        task = self._stream_tasks.get(stream_id)
        if finished_task is not None and task is not finished_task:
            task = None
        elif task is not None:
            self._stream_tasks.pop(stream_id, None)
            self._active_stream_count = max(0, self._active_stream_count - 1)
            if not task.done() and task is not asyncio.current_task():
                task.cancel()

        sender = self._senders.pop(stream_id, None)
        recipient = self._recipients.pop(stream_id, None)
        stream = self.root_stream.children.pop(stream_id, None)
        advances_watermark = (
            stream is not None and stream.state != StreamState.IDLE
        )
        owned = (task is not None or sender is not None
                 or recipient is not None or stream is not None)

        if sender is not None:
            retire = getattr(sender, 'retire_stream', None)
            if retire is not None:
                retire()
            else:
                sender.mark_client_gone()
        if recipient is not None:
            recipient.put_disconnect()
            self._release_recipient_credit(recipient, additional_credit)
        elif additional_credit:
            self._release_recipient_credit(None, additional_credit)

        if stream_id in self._ws_stream_ids:
            self._ws_stream_ids.remove(stream_id)
            self._ws_stream_count = max(0, self._ws_stream_count - 1)
        elif is_ws_hint and owned:
            self._ws_stream_count = max(0, self._ws_stream_count - 1)

        if owned or via_rst:
            self._mark_closed(
                stream_id, via_rst=via_rst, by_peer=by_peer,
                advances_watermark=advances_watermark)

    def _retire_all_streams(self) -> None:
        """Release connection work while preserving connection codecs."""
        for task in tuple(self._credit_flush_tasks):
            if not task.done():
                task.cancel()
        self._credit_flush_tasks.clear()
        stream_ids = (
            set(self.root_stream.children)
            | set(self._stream_tasks)
            | set(self._senders)
            | set(self._recipients)
        )
        for stream_id in stream_ids:
            self._retire_stream(stream_id, via_rst=False)

    async def receive(self) -> bytes:
        """Read one HTTP/2 frame from the connection."""
        assert self._reader is not None, "receive() called with no reader"
        try:
            data = await self._reader.readexactly(9)
        except (IncompleteReadError, asyncio.IncompleteReadError):
            return b''
        size = int.from_bytes(data[:3], 'big', signed=False)
        if size > DEFAULT_MAX_FRAME_SIZE:
            # RFC 9113 §4.2.  Hand back the 9-byte header alone rather than
            # buffer an attacker-declared payload of up to 16 MiB to reject it.
            self._oversize_frame_len = size
            return data
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
        cfg = get_settings()

        # RFC 8441 §3 — never invite Extended CONNECT unless the operator has
        # opted in, because a peer that never sees the bit will not send it.
        await self.send_frame(self.factory.settings(
            enable_connect_protocol=cfg.h2_enable_websocket,
            initial_window_size=cfg.h2_initial_window_size,
            max_concurrent_streams=self.max_concurrent_streams,
            max_header_list_size=(self.factory.decoder.max_header_list_size
                                  if self._field_block_max else None),
        ))
        self._ws_over_h2_enabled = cfg.h2_enable_websocket
        self._force_asgi = cfg.force_asgi_scope
        logger.info(
            'HTTP/2 SETTINGS sent: initial_window_size=%d max_concurrent_streams=%d',
            cfg.h2_initial_window_size, self.max_concurrent_streams,
        )

        conn_increment = cfg.h2_connection_window_size - DEFAULT_INITIAL_WINDOW_SIZE
        if conn_increment > 0:
            await self.send_frame(self.factory.window_update(0, conn_increment))

        self._recipients.clear()

        self._last_frame_at = asyncio.get_running_loop().time()
        self._probe_sent_at = None

        try:
            async with asyncio.TaskGroup() as tg:
                self._task_group = tg
                liveness = self._liveness = Deadline(self._check_liveness)
                if self._h2_idle_timeout > 0 or self._header_timeout > 0:
                    self._check_liveness()
                try:
                    await self._frame_loop(tg)
                finally:
                    liveness.disarm()
        finally:
            # Both refer back to this actor.
            self._task_group = self._liveness = None

    async def _count_emitted_rst(self) -> None:
        """Count a reset *this server* sent, on the inbound Rapid Reset meter.

        Why ours count too, and why losing the connection is the intended
        consequence for a client that keeps tripping a legitimate limit:
        ``docs/about/rfc9113-implementation.md`` §6.4.
        """
        if not self._rst_meter.hit():
            return
        log_cap_hit('frame_rate',
                    requested=self._rst_meter.count, limit=self._rst_meter.limit,
                    protocol='http2')
        logger.warning(
            'server-emitted RST_STREAM rate limit exceeded (%d in %ss) — '
            'the peer is provoking resets faster than the budget allows',
            self._rst_meter.count, self._rst_meter.window)
        await self._connection_error(
            ErrorCodes.ENHANCE_YOUR_CALM, 'server-emitted RST_STREAM rate limit exceeded')

    def _check_liveness(self) -> None:
        """Enforce header, idle and PING deadlines by closing the writer.

        Scanner callback.  Do not cancel a partially read frame; closing wakes
        the reader for ordered stream teardown.  The applicable deadline is
        chosen afresh each time, since frames move it while it waits.
        """
        now = asyncio.get_running_loop().time()
        if self._probe_sent_at is not None:
            due, expired = self._probe_sent_at + self._h2_ping_timeout, 'probe'
        elif self._block_opened_at is not None and self._header_timeout > 0:
            due, expired = self._block_opened_at + self._header_timeout, 'header'
        elif self._h2_idle_timeout > 0:
            due, expired = self._last_frame_at + self._h2_idle_timeout, 'idle'
        else:
            # Nothing to watch until the frame loop opens a header block.
            self._liveness.arm(self._header_timeout)
            return
        if due > now:
            self._liveness.arm(due - now)
        elif expired == 'header':
            _schedule_group_task(self._task_group, self._end_for_stalled_header_block())
        elif expired == 'probe':
            _schedule_group_task(self._task_group, self._end_for_unresponsive_peer())
        else:
            self._probe_sent_at = now
            _schedule_group_task(self._task_group, self._probe_peer())
            self._check_liveness()

    async def _probe_peer(self) -> None:
        """Ask a silent peer whether it is still there (RFC 9113 §6.7)."""
        if _DEBUG:
            logger.debug('HTTP/2 idle %.1fs — probing with PING',
                         self._h2_idle_timeout)
        with contextlib.suppress(Exception):
            await self.send_frame(self.factory.create(
                FrameTypes.PING, 0, 0, data=b'\x00' * 8))

    async def _end_for_stalled_header_block(self) -> None:
        """A header block the peer opened and never finished.

        A connection error rather than a stream reset, even though one stream
        is nominally involved — see ``docs/about/rfc9113-implementation.md``
        §10.5, "Unfinished header block held open".
        """
        log_cap_hit('header_timeout',
                    requested=self._header_timeout, limit=self._header_timeout,
                    protocol='http2')
        logger.warning('HTTP/2 header block incomplete after %.1fs — GOAWAY',
                       self._header_timeout)
        await self._connection_error(ErrorCodes.ENHANCE_YOUR_CALM,
                                     'header block incomplete')

    async def _end_for_unresponsive_peer(self) -> None:
        """The probe went unanswered: the peer is gone, not merely quiet.

        ``NO_ERROR`` because nothing was violated — see
        ``docs/about/rfc9113-implementation.md`` §6.7.
        """
        logger.info('HTTP/2 peer did not answer the liveness PING in %.1fs '
                    '— GOAWAY', self._h2_ping_timeout)
        await self._connection_error(ErrorCodes.NO_ERROR, 'liveness PING unanswered')

    async def _frame_loop(self, tg: asyncio.TaskGroup) -> None:
        """Read frames and dispatch stream tasks until EOF or GOAWAY."""
        _tasks_since_yield = 0
        _yield_every = self._frame_yield_every
        # ``loop.time()``: the liveness watchdog sleeps on the loop's clock.
        _loop_time = asyncio.get_running_loop().time

        while data := await self.receive():
            self._last_frame_at = _loop_time()
            self._probe_sent_at = None
            if self._goaway_sent:
                # A stream task blocked in receive() needs http.disconnect, or
                # the TaskGroup waits on it forever.
                _signal_recipients(self._recipients)
                return
            if self._oversize_frame_len:
                # The unread payload is discarded on close.
                n = self._oversize_frame_len
                self._oversize_frame_len = 0
                await self._connection_error(
                    ErrorCodes.FRAME_SIZE_ERROR,
                    f'frame length {n} exceeds SETTINGS_MAX_FRAME_SIZE '
                    f'{DEFAULT_MAX_FRAME_SIZE}')
                _signal_recipients(self._recipients)
                return
            try:
                frame = self.factory.load(data)
            except Exception as exc:
                await self._unloadable(exc)
                continue    # let the peer read the GOAWAY before we close
            stream = await self._admit(frame)
            if stream is SKIP:
                continue
            frame_type = frame.FrameType()
            if frame_type is FrameTypes.HEADERS or frame_type is FrameTypes.CONTINUATION:
                if frame_type is FrameTypes.CONTINUATION or not frame.end_headers:
                    frame = await self._assemble(frame, stream)
                    if frame is None or stream is None:
                        continue    # incomplete, or decoded for a stream we reset
                await self._complete_header_block(
                    frame, stream, self.make_sender(stream.stream_id), tg)
                if _yield_every > 0:
                    _tasks_since_yield += 1
                    if _tasks_since_yield >= _yield_every:
                        await asyncio.sleep(0)
                        _tasks_since_yield = 0
            elif frame_type is FrameTypes.DATA:
                await self._on_data_frame(frame, stream)
            elif frame_type is FrameTypes.WINDOW_UPDATE:
                await self.on_window_update(frame)
            elif frame_type is FrameTypes.SETTINGS:
                await self.on_settings(frame)
            elif frame_type is FrameTypes.PING:
                await self.on_ping(frame)
            elif frame_type is FrameTypes.RST_STREAM:
                await self.on_rst_stream(frame, stream)
            elif frame_type is FrameTypes.PRIORITY:
                await self.on_priority(frame)
            elif frame_type is FrameTypes.PRIORITY_UPDATE:
                self.on_priority_update(frame)
            elif frame_type is FrameTypes.GOAWAY:
                await self._on_goaway_frame(self._last_peer_stream_id)
                return

        _signal_recipients(self._recipients)

    def _spawn_stream_task(
        self,
        tg: asyncio.TaskGroup,
        stream_id: int,
        conn: Connection,
        recipient,
        send,
        log_record,
    ) -> None:
        """Spawn the StreamActor that runs one stream's app dispatch.

        *conn* is always the native [`Connection`][]; the compat lane's ASGI
        scope is derived beyond here, at the app boundary, and nowhere else.

        Registers the task and increments ``_active_stream_count`` only after
        task creation succeeds.  The common done-callback retirement reverses
        both registrations exactly once when the task finishes.

        A ``BB_REQUEST_TIMEOUT`` expiry sends RST_STREAM CANCEL and lets the
        task complete normally, so it does not cancel the TaskGroup.
        """
        # StreamActor failure resets with INTERNAL_ERROR instead of leaving a silent stream.
        def _start_stream():
            return StreamActor(
                stream_id=stream_id,
                conn=conn,
                receive=recipient,
                send=send,
                app=self.app,
                aggregator=self._aggregator,
                http2_actor=self,
                log_record=log_record,
                force_asgi=self._force_asgi,
            ).run()

        timeout = self._request_timeout
        if timeout > 0:
            _sp = conn.path

            async def _timed(make=_start_stream, sid=stream_id, t=timeout,
                             sp=_sp):
                try:
                    await asyncio.wait_for(make(), timeout=t)
                except asyncio.TimeoutError:
                    logger.warning(
                        'Stream %d timed out after %.1fs — RST_STREAM CANCEL', sid, t)
                    log_cap_hit('request_timeout',
                                requested=t, limit=t,
                                scope_path=sp, protocol='http2')
                    await self.send_frame(self.factory.rst_stream(sid, ErrorCodes.CANCEL))
            make_final = _timed
        else:
            make_final = _start_stream

        if self._stream_semaphore is not None:
            task = _schedule_group_task(
                tg,
                _run_when_stream_cap_admits(make_final, self._stream_semaphore))
        else:
            task = _schedule_group_task(tg, make_final())
        if task is None:
            # HEADERS arrived in the turn the connection went away; no peer
            # left to RST_STREAM.
            if _DEBUG:
                logger.debug('stream %d not started: connection closing',
                             stream_id)
            self._retire_stream(stream_id, via_rst=False)
            return

        self._active_stream_count += 1
        self._stream_tasks[stream_id] = task
        task.add_done_callback(self._make_done_cb(stream_id))

    def _apply_priority_and_extensions(self, stream: 'Stream',
                                       conn: Connection) -> None:
        """Resolve stream priority and attach the H/2 request extensions.

        Writes to the native [`Connection`][] on every lane; the compat
        lane's scope picks the dict up by reference when it is derived.
        """
        priority = _resolve_priority(stream, conn)
        conn.extensions = _build_h2_extensions(
            stream.stream_id, priority,
            self._peer_initial_window_size, self._conn_window.size,
            self._peer_enable_push)

    async def _complete_header_block(
        self,
        header_frame,
        stream: 'Stream',
        send,
        tg: asyncio.TaskGroup,
    ) -> bool:
        """Apply one completed request field section, independent of framing.

        ``header_frame`` is always the opening HEADERS frame.  Its END_STREAM
        bit owns the request-body transition even when END_HEADERS arrives on
        a later CONTINUATION frame.
        """
        if stream.state == StreamState.HALF_CLOSED_REMOTE:
            await self.send_frame(self.factory.rst_stream(
                stream.stream_id, ErrorCodes.STREAM_CLOSED))
            return True

        if stream.conn is not None:
            # A second field section is trailers: it must end the request,
            # carry no pseudo-header or prohibited field (RFC 9110 §6.5.1),
            # and reach no handler; anything else earns the head's verdict.
            prohibited = next((name for name, _ in header_frame.headers
                               if name in PROHIBITED_TRAILER_FIELDS), None)
            if (not header_frame.end_stream or header_frame.malformed
                    or header_frame.pseudo_headers or prohibited is not None):
                reason = (header_frame.malformed_reason
                          or ('pseudo-header in trailer section'
                              if header_frame.pseudo_headers
                              else f'prohibited trailer field {prohibited!r}'
                              if prohibited is not None
                              else 'section does not end the request'))
                if _DEBUG:
                    logger.debug(
                        'Stream %d refused trailing field section — %s',
                        stream.stream_id, reason)
                await self.send_frame(self.factory.rst_stream(
                    stream.stream_id, ErrorCodes.PROTOCOL_ERROR))
                return True
            stream.on_data_received(end_stream=True)
            recipient = self._recipients.get(stream.stream_id)
            if recipient is not None:
                recipient.put_end_of_stream()
            return True

        if self._active_stream_count >= self.max_concurrent_streams:
            # Refused only once the block is decoded, so the connection-wide
            # HPACK state stays in sync — rfc9113-implementation.md §5.1.2.
            log_cap_hit('h2_max_concurrent_streams',
                        requested=self._active_stream_count + 1,
                        limit=self.max_concurrent_streams,
                        protocol='http2')
            await self.send_frame(
                self.factory.rst_stream(stream.stream_id, ErrorCodes.REFUSED_STREAM))
            return True

        conn = parse_headers(header_frame)
        # RFC 9113 §8.1.1 / §8.2.1 — rejected here rather than dispatched.
        # parse_payload sets the flag for field-level violations; parse_headers
        # sets it for missing or empty required pseudo-headers.
        if getattr(header_frame, 'malformed', False):
            if _DEBUG:
                logger.debug('Stream %d malformed HEADERS — %s',
                             stream.stream_id, header_frame.malformed_reason)
            await self.send_frame(
                self.factory.rst_stream(stream.stream_id, ErrorCodes.PROTOCOL_ERROR))
            return True

        assert conn is not None  # not malformed → parse_headers built a Connection
        self._fill_scope_connection(conn)
        try:
            # RFC 9113 §8.1.1: a length other than one canonical number is malformed.
            stream.expected_content_length = parse_content_length(
                conn.headers._index.get(b'content-length', ()), canonical=True)
        except ValueError:
            await self.send_frame(
                self.factory.rst_stream(stream.stream_id, ErrorCodes.PROTOCOL_ERROR))
            return True

        if conn.type == 'websocket':
            # RFC 8441 Extended CONNECT.  Threaded natively even under
            # BB_FORCE_ASGI_SCOPE — the WS extras live on the Connection, and
            # that lane round-trips only the HTTP app boundary.  Reaching here
            # with the setting off means a non-conforming peer: we never
            # advertised ENABLE_CONNECT_PROTOCOL.
            stream.conn = conn
            if not self._ws_over_h2_enabled:
                await self.send_frame(self.factory.rst_stream(
                    stream.stream_id, ErrorCodes.PROTOCOL_ERROR))
                return True
            stream.on_headers_received(end_stream=False)
            log_record = _start_record(conn)
            await self._handle_h2_websocket(stream, tg, log_record)
            return True

        stream.conn = conn
        # Before anything can refuse the request and write its own response:
        # that response owes the same content rules as the application's.
        self.make_sender(stream.stream_id,
                         head_mode=method_is(conn.method, 'HEAD'))

        # Keep the common admission check inline; the refusal path rechecks
        # and enforces the limit.
        declared = stream.expected_content_length
        if (declared is not None and declared > self._max_body_size > 0
                and await self._refuse_oversized_declared_body(
                    stream, conn, send)):
            return True

        self._apply_priority_and_extensions(stream, conn)
        stream_recipient = self._make_stream_recipient(stream.stream_id)
        self._recipients[stream.stream_id] = stream_recipient
        stream.on_headers_received(end_stream=bool(header_frame.end_stream))
        if header_frame.end_stream:
            # No body: skip the queue allocation and let the recipient
            # synthesise the empty http.request event if one is asked for.
            stream_recipient.mark_end_of_stream_on_headers()
        # Capture native sends inside sender arms; None means no record consumer.
        log_record = _open_record(conn, self._aggregator)
        send._log_record = log_record
        self._spawn_stream_task(tg, stream.stream_id, conn, stream_recipient, send, log_record)
        return True

    async def _refuse_oversized_declared_body(self, stream, conn, send) -> bool:
        """Answer an oversized declared body with 413, then RST_STREAM(NO_ERROR).

        Keep sibling streams alive. Undeclared bodies are counted by HTTP2Recipient.
        """
        declared = stream.expected_content_length
        cap = self._max_body_size
        if not cap or declared is None or declared <= cap:
            return False
        logger.warning(
            '413 Content Too Large — stream %d declares %d bytes, '
            'BB_MAX_BODY_SIZE=%d', stream.stream_id, declared, cap)
        log_cap_hit('max_body_size', requested=declared, limit=cap,
                    scope_path=conn.path, protocol='http2')
        await send(b'413 Content Too Large',
                   HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                   [(b'content-type', b'text/plain')])
        await self.send_frame(
            self.factory.rst_stream(stream.stream_id, ErrorCodes.NO_ERROR))
        return True

    async def _on_data_frame(self, frame, stream: 'Stream') -> None:
        """Handle a DATA frame: state, content-length accounting, delivery.

        Every recipient that can receive DATA credits on *consumption* from its
        own callback, so nothing is credited here (RFC 9113 §6.9.1,
        ``docs/about/rfc9113-implementation.md``).
        """
        if stream.state in (StreamState.HALF_CLOSED_REMOTE, StreamState.CLOSED):
            self._retire_stream(
                stream.stream_id, via_rst=True,
                additional_credit=frame.length)
            await self.send_frame(
                self.factory.rst_stream(stream.stream_id, ErrorCodes.STREAM_CLOSED))
            return

        # RFC 9113 §8.1.1 — the sum of DATA payloads must equal the declared
        # content-length, padding excluded.  Excess is caught per frame,
        # deficit when END_STREAM arrives.
        stream.received_data_bytes += len(frame.payload)
        expected = stream.expected_content_length
        if expected is not None:
            if stream.received_data_bytes > expected or (
                frame.end_stream and stream.received_data_bytes != expected
            ):
                self._retire_stream(
                    stream.stream_id, via_rst=True,
                    additional_credit=frame.length)
                await self.send_frame(self.factory.rst_stream(
                    stream.stream_id, ErrorCodes.PROTOCOL_ERROR))
                return

        stream.on_data_received(end_stream=bool(frame.end_stream))
        if stream.stream_id in self._recipients:
            recipient = self._recipients[stream.stream_id]
            if not recipient.put_DATAFrame(frame):
                # Refused: an overrun of the advertised window, a degenerate
                # tiny-frame flood, or a body limit.  Tell the recipient as
                # well, so a handler parked in ``receive()`` for a body that
                # will never continue unwinds now rather than at the timeout.
                self._retire_stream(
                    stream.stream_id, via_rst=True,
                    additional_credit=frame.length)
                await self.send_frame(
                    self.factory.rst_stream(stream.stream_id, ErrorCodes.ENHANCE_YOUR_CALM))
        else:
            logger.warning('DATA for stream %d but no recipient found', stream.stream_id)

    def on_priority_update(self, frame) -> None:
        """RFC 9218 §7.1: record a stream's priority hint; nothing schedules on it."""
        hint = frame.parsed_priority
        stream_id = frame.prioritized_stream_id
        stream = self.find_stream(stream_id)
        if stream is None:
            if self._is_closed_stream(stream_id)[0]:
                return
            # §7: retain idle hints only up to the concurrent stream bound.
            if len(self.root_stream.children) >= self.max_concurrent_streams:
                log_cap_hit('h2_priority_update_buffer',
                            requested=len(self.root_stream.children) + 1,
                            limit=self.max_concurrent_streams, protocol='http2')
                return
            stream = self.root_stream.add_child(stream_id)
        stream.priority_hint = hint
        # A late hint must reach the extensions the app already holds.
        conn = stream.conn
        if conn is not None and conn.extensions is not None:
            conn.extensions['http.response.priority'] = hint

    async def _on_goaway_frame(self, last_stream_id: int) -> None:
        """Send GOAWAY with the supplied peer-stream id and signal all recipients.
        """
        await self.send_frame(self.factory.goaway(last_stream_id))
        _signal_recipients(self._recipients)

    async def _handle_h2_websocket(
        self,
        stream: 'Stream',
        tg: asyncio.TaskGroup,
        log_record,
    ) -> None:
        """Bootstrap a WebSocket connection over HTTP/2 per RFC 8441.

        The 200 HEADERS response is deferred into ``conn._ws['send_101']`` —
        the same key the H/1.1 path uses — so ``WebSocketActor`` is shared
        between the two transports unchanged.
        """
        from .conn_id import new_connection_id  # noqa: PLC0415
        from .websocket_actor import WebSocketActor  # noqa: PLC0415
        from .http2_ws import HTTP2StreamReader, HTTP2WSWriter  # noqa: PLC0415

        # Without this per-connection cap a peer may hold
        # ``max_concurrent_streams`` idle WS streams.  ``0`` disables it.
        cfg = get_settings()
        ws_cap = cfg.h2_ws_max_streams_per_connection
        if ws_cap > 0 and self._ws_stream_count >= ws_cap:
            _ws_conn = stream.conn
            log_cap_hit('h2_ws_max_streams_per_connection',
                        requested=self._ws_stream_count + 1,
                        limit=ws_cap,
                        scope_path=_ws_conn.path,
                        protocol='h2-ws')
            await self.send_frame(self.factory.rst_stream(
                stream.stream_id, ErrorCodes.REFUSED_STREAM))
            return

        conn = stream.conn
        assert conn is not None
        conn.connection_id = self._connection_id or new_connection_id()
        stream_send = self.make_sender(stream.stream_id,
                                       head_mode=method_is(conn.method,
                                                           'HEAD'))

        # RFC 7692 permessage-deflate negotiation, shared with the HTTP/1.1
        # handshake: the offer decides, the answer rides the 200, and the
        # parameters reach WebSocketActor through ``conn._ws``.
        from .permessage_deflate import negotiate_offer as _negotiate_deflate  # noqa: PLC0415
        deflate_params, deflate_response = _negotiate_deflate(conn.headers)

        async def _ws_send_200(subprotocol=None, app_headers=None):
            headers = []
            if subprotocol:
                sp = subprotocol if isinstance(subprotocol, str) else subprotocol.decode()
                headers = [(b'sec-websocket-protocol', sp.encode())]
            if deflate_response is not None:
                headers.append((b'sec-websocket-extensions', deflate_response))
            headers.extend(app_headers or ())
            # Flushed now, not through http.response.start: HTTP2Sender
            # coalesces HEADERS with the first DATA, and an RFC 8441 accept has
            # no body, so the HEADERS would never leave and the handshake would
            # hang.  No END_STREAM — the stream stays open for WS DATA frames.
            await stream_send.send_response_headers(HTTPStatus(200), headers)

        conn._ws = {'send_101': _ws_send_200, 'deflate': deflate_params}

        ws_reader = HTTP2StreamReader(
            self._inbound_stream_window,
            self._make_consume_credit_callback(stream.stream_id))
        ws_writer = HTTP2WSWriter(stream_send)
        self._recipients[stream.stream_id] = ws_reader

        aggregator = self._aggregator
        if aggregator is None:
            from ..event import EventDispatcher  # noqa: PLC0415
            from ..event_aggregator import EventAggregator  # noqa: PLC0415
            aggregator = EventAggregator(EventDispatcher())

        log_record.status = 200
        ws_actor = WebSocketActor(
            ws_reader, ws_writer, conn, self.app, aggregator,
            peername=self._peername, sockname=self._sockname, ssl=self._ssl,
        )

        async def _run_ws():
            try:
                await ws_actor.run()
            finally:
                _close_ws_record(log_record, ws_actor._disconnect_code)

        task = _schedule_group_task(tg, _run_ws())
        if task is None:
            self._retire_stream(stream.stream_id, via_rst=False)
            return
        self._ws_stream_ids.add(stream.stream_id)
        self._ws_stream_count += 1
        self._active_stream_count += 1
        self._stream_tasks[stream.stream_id] = task
        task.add_done_callback(
            self._make_done_cb(stream.stream_id, is_ws=True))

    async def _handle_push(self, event: NativeResponse,
                           parent_stream_id: int) -> None:
        """Handle a native promised request.

        RFC 9113 §8.4 / §8.4.1 (safe, cacheable, body-less, always ``GET``)
        and §6.6 for the PUSH_PROMISE frame.  The ``:path`` split uses the
        same ``_split_h2_path`` as request HEADERS, so a pushed request gets
        the identical ``path`` / ``raw_path`` / ``query_string`` contract.
        """
        if not self._peer_enable_push:
            logger.warning(
                'HTTP2Actor: dropping http.response.push on stream %d — '
                'peer SETTINGS_ENABLE_PUSH=0', parent_stream_id)
            return

        parent_stream = self.root_stream.find_child(parent_stream_id)
        if parent_stream is None or self._task_group is None:
            logger.warning(
                'HTTP2Actor: dropping http.response.push on stream %d — '
                'parent or connection task group is no longer live',
                parent_stream_id)
            return

        from .parser import _split_h2_path  # noqa: PLC0415

        push_stream_id = self._allocate_push_stream_id()
        path = event.push

        parent = parent_stream.conn
        # Plain attribute reads, never ``.get()`` on a scope: under
        # BB_FORCE_ASGI_SCOPE that reaches a header *list* and raises
        # AttributeError for every push.
        if parent is not None:
            parent_headers = parent.headers
            parent_scheme = parent.scheme
            _parent_client = parent.client
        else:
            parent_headers, parent_scheme, _parent_client = Headers([]), 'https', None
        # §8.3.1 maps ``:authority`` into ``host``, so a dispatched parent
        # always carries one; the fallback only covers a parent with none.
        raw_authority = parent_headers.get(b'host') or b'localhost'
        authority = raw_authority.decode()

        from ..protocol.frame_types import PseudoHeaders  # noqa: PLC0415
        pseudo = {
            PseudoHeaders.METHOD:    'GET',
            PseudoHeaders.PATH:      path,
            PseudoHeaders.SCHEME:    parent_scheme,
            PseudoHeaders.AUTHORITY: authority,
        }
        # NativeResponse holds lowercase tchar names, trimmed values and no
        # host; the promised request takes its host from the authority above.
        regular = list(event._header or ())

        pp = self.factory.push_promise(parent_stream_id, push_stream_id, pseudo, regular)
        # The peer may reset a promised id as soon as the frame reaches its
        # transport, including while our write waits for drain.
        push_stream = self.root_stream.add_child(push_stream_id)
        push_stream.state = StreamState.HALF_CLOSED_REMOTE
        try:
            await self.send_frame(pp)
        except asyncio.CancelledError:
            if (self.root_stream.find_child(push_stream_id) is push_stream
                    and not self._goaway_sent and not self._writer.peer_gone):
                # Cancellation can arrive from a reset of the parent after
                # PUSH_PROMISE reached the transport but before drain returned.
                try:
                    await self.send_frame(self.factory.rst_stream(
                        push_stream_id, ErrorCodes.CANCEL))
                except OSError:
                    # A best-effort reset cannot replace the cancellation that
                    # owns the parent stream's terminal state.
                    self._retire_stream(push_stream_id, via_rst=True)
                    if _DEBUG:
                        logger.debug(
                            'promised-stream reset write failed', exc_info=True)
            else:
                self._retire_stream(push_stream_id, via_rst=False)
            raise
        except Exception:
            self._retire_stream(push_stream_id, via_rst=False)
            raise
        if (self.root_stream.find_child(push_stream_id) is not push_stream
                or self._writer.peer_gone):
            self._retire_stream(push_stream_id, via_rst=False)
            return

        _pushed_path, _pushed_raw_path, _pushed_query = _split_h2_path(path)
        pushed_conn = Connection(
            method='GET',
            path=_pushed_path,
            raw_path=_pushed_raw_path,
            headers=Headers.from_lowered([(b'host', raw_authority), *regular]),
            query_string=_pushed_query,
            http_version='2',
            scheme=parent_scheme,
            type='http',
            client=tuple(_parent_client) if _parent_client else None,
            extensions=_build_h2_extensions(
                push_stream_id, _DEFAULT_PRIORITY,
                self._peer_initial_window_size,
                self._conn_window.size,
                peer_push_permitted=False),
        )
        push_recipient = RecipientFactory.http2(
            queue_depth=self._stream_queue_depth,
            max_body=self._max_body_size,
            min_rate=self._min_body_rate,
            min_rate_grace=self._min_body_rate_grace)
        # §8.4.1: no body, so the same lazy-queue path as an END_STREAM GET.
        push_recipient.mark_end_of_stream_on_headers()
        self._recipients[push_stream_id] = push_recipient
        push_sender = SenderFactory.http2(
            self._writer, self.factory, push_stream_id, push_callback=None,
            conn_window=self._conn_window,
            flow_control_timeout=self._write_timeout)
        self._senders[push_stream_id] = push_sender
        push_stream.conn = pushed_conn
        log_record = _start_record(pushed_conn)
        push_sender._log_record = log_record
        capturing_send = push_sender

        self._spawn_stream_task(
            self._task_group, push_stream_id, pushed_conn,
            push_recipient, capturing_send, log_record,
        )

    async def _handle(self, msg: Message) -> None:
        raise NotImplementedError
