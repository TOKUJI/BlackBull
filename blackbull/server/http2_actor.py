"""HTTP/2 Actor classes for the BlackBull actor model.

HTTP2Actor drives the HTTP/2 connection state machine for one TCP connection.
StreamActor owns the lifetime of a single HTTP/2 stream.
"""
import asyncio
import contextlib
import inspect
import logging
from collections.abc import Awaitable, Callable
from http import HTTPStatus
from typing import Protocol, runtime_checkable

from ..actor import Actor, Message
from ..event_aggregator import EventAggregator
from ..logger import log, debug_gate
from ..protocol.frame import FrameFactory
from ..protocol.frame_types import (
    ErrorCodes, FrameBase, FrameTypes,
    DEFAULT_INITIAL_WINDOW_SIZE, DEFAULT_MAX_FRAME_SIZE,
)
from ..protocol.stream import Stream, StreamState
from ..connection import Connection
from ..headers import Headers
from .parser import parse_headers
from .cap_log import log_cap_hit
from .rate_window import RateWindow
from .recipient import (AbstractReader, IncompleteReadError,
                        HTTP2Recipient, RecipientFactory,
                        _HTTP2_STREAM_QUEUE_DEPTH)
from .response import ResponderFactory
from .sender import (AbstractWriter, ConnectionWindow, FlowControlStalled,
                     SenderFactory)
from .access_log import (
    close_record as _close_record,
    close_ws_record as _close_ws_record,
    open_record as _open_record,
    start_record as _start_record,
)
from ..asgi import (ASGIEvent, ASGIReceiveCallable, ASGISendCallable,
                    HTTPResponsePushEvent)
from .http1_actor import RequestActor

logger = logging.getLogger(__name__)
#: Read once at import; the cost this buys is measured in
#: [`blackbull.logger.debug_gate`][blackbull.logger.debug_gate].
_DEBUG = debug_gate(logger)



@runtime_checkable
class _StreamRecipient(Protocol):
    """Duck-type interface shared by HTTP2Recipient and HTTP2WSReader."""
    def put_disconnect(self) -> None: pass
    def put_DATAFrame(self, frame) -> bool: pass


def _req_headers(conn: Connection) -> Headers:
    """The one source both header readers share, so they cannot drift apart."""
    return conn.headers


def _extract_content_length(conn: Connection) -> int | None:
    """Return the request's content-length, or None if absent or unparseable.

    The "must equal the sum of DATA payloads" rule the caller enforces is
    RFC 9113 §8.1.1 (RFC 7540 located it at §8.1.2.6).
    """
    for name, value in _req_headers(conn):
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


_DEFAULT_PRIORITY: dict[str, int | bool] = {'urgency': 3, 'incremental': False}

_CLOSED_STREAMS_CAP = 1024


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
    """Owns one HTTP/2 stream.

    Single-shot like RequestActor: run() processes one stream and returns.
    Delegates to RequestActor for ASGI dispatch.
    Supervisor strategy: isolate — RST_STREAM on unhandled error.
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
            await self._http2_actor.send_frame(
                self._http2_actor.factory.rst_stream(
                    self._stream_id, ErrorCodes.INTERNAL_ERROR)
            )
        finally:
            # ``None`` on the baseline hot path; ``_close_record`` tolerates it.
            _close_record(self._log_record)

    async def _handle(self, msg: Message) -> None:
        raise NotImplementedError


class HTTP2Actor(Actor):
    """Drives the HTTP/2 connection state machine for one connection.

    Supervisor strategy: propagate — framing errors send GOAWAY and raise,
    surfacing to the caller.

    If *aggregator* is ``None`` the actor dispatches events directly through
    ``app._dispatcher`` instead.
    """

    # RFC 9113 §4.2 — oversize is a connection error for these and a stream
    # error for the rest.  A future frame type that can alter connection
    # state (carries a header block, or targets stream 0) belongs here too.
    _FRAME_SIZE_CONNECTION_ERROR_TYPES: frozenset[FrameTypes] = frozenset({
        FrameTypes.HEADERS,
        FrameTypes.CONTINUATION,
        FrameTypes.PUSH_PROMISE,
        FrameTypes.SETTINGS,
    })

    # Frame types that MUST NOT appear on stream 0 → connection
    # PROTOCOL_ERROR.  One lookup rather than a check per frame type: a
    # per-type check is a list to keep complete, and RST_STREAM (§6.4) and
    # PUSH_PROMISE (§6.6) are the two that fall off it.
    _STREAM_ONLY_FRAME_TYPES: frozenset[FrameTypes] = frozenset({
        FrameTypes.DATA,          # RFC 9113 §6.1
        FrameTypes.HEADERS,       # RFC 9113 §6.2
        FrameTypes.PRIORITY,      # RFC 9113 §6.3
        FrameTypes.RST_STREAM,    # RFC 9113 §6.4
        FrameTypes.PUSH_PROMISE,  # RFC 9113 §6.6
        FrameTypes.CONTINUATION,  # RFC 9113 §6.10
    })

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
        self._header_max_total: int = _cfg.header_max_total
        self._max_body_size: int = _cfg.max_body_size
        # These limits are connection-scoped on purpose: every one is
        # process-wide configuration, and a stream is a request — reading them
        # per stream would put a settings lookup, and the function-level import
        # that reaches it, on the per-request path.
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
        # RFC 9113 §6.5.2 — ENABLE_PUSH's initial value is 1.
        self._peer_enable_push: bool = True
        self._task_group: asyncio.TaskGroup | None = None

        # The time axis.  HTTP/2 cannot borrow HTTP/1.1's ConnectionDeadline:
        # that cancels the task parked in the read, and this actor's read is a
        # frame loop the server usually intends to keep.  Hence
        # ``_liveness_watchdog``, which observes rather than interrupts.
        self._h2_idle_timeout: float = _cfg.h2_idle_timeout
        self._h2_ping_timeout: float = _cfg.h2_ping_timeout
        self._header_timeout: float = _cfg.header_timeout
        self._last_frame_at: float = 0.0
        # Set while the peer owes CONTINUATION on an unterminated block.
        self._header_block_since: float | None = None
        self._probe_sent_at: float | None = None

        # Flow-control state — updated by SettingsResponder /
        # WindowUpdateResponder (rfc9113-implementation.md §5.2, §6.9.1).
        self._peer_initial_window_size: int = DEFAULT_INITIAL_WINDOW_SIZE
        self._conn_window = ConnectionWindow(DEFAULT_INITIAL_WINDOW_SIZE)
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

        # The frame loop exits on the next iteration once this is set, which is
        # what gives the GOAWAY time to flush before the connection closes.
        self._goaway_sent: bool = False

        # RFC 9113 §5.1.1 — peer-initiated stream IDs must strictly increase.
        self._last_peer_stream_id: int = 0

        # RFC 8441; run() sets both of these from the environment.  The
        # defaults here are for tests that drive the frame handlers directly.
        self._ws_over_h2_enabled: bool = False
        self._force_asgi: bool = False

        # A meter per counted thing, not one shared budget, so a peer may
        # legitimately spend its whole allowance of each without the types
        # competing.  What each defends and why a byte budget cannot:
        # ``BB_FRAME_RATE_LIMIT`` in docs/reference/env-vars.md.
        _rate, _window = _cfg.frame_rate_limit, _cfg.frame_rate_window
        self._rst_meter = RateWindow(_rate, _window)          # CVE-2023-44487
        self._ping_meter = RateWindow(_rate, _window)         # CVE-2019-9512
        self._settings_meter = RateWindow(_rate, _window)     # CVE-2019-9515
        self._empty_frame_meter = RateWindow(_rate, _window)  # CVE-2019-9518

        # RFC 8441 stream-exhaustion guard, capped at
        # ``cfg.h2_ws_max_streams_per_connection``.
        self._ws_stream_count: int = 0

        # RFC 9113 §5.1 late-frame validation: stream_id → closed-via-RST.
        # Bounded, so a connection cycling millions of streams (gRPC) cannot
        # grow it; evicted ids stay CLOSED via the high-water mark.  Holding
        # this rather than the Stream node is what keeps find_child O(1).
        self._closed_streams: dict[int, bool] = {}
        self._closed_high_water: int = 0
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

    def make_sender(self, stream_id: int):
        if stream_id not in self._senders:
            sender = SenderFactory.http2(
                self._writer, self.factory, stream_id,
                push_callback=self._handle_push,
                conn_window=self._conn_window,
                # What the peer has currently granted, not the RFC default:
                # SETTINGS received before this stream opened may have moved it.
                initial_window=self._peer_initial_window_size,
                flow_control_timeout=self._write_timeout,
            )
            self._senders[stream_id] = sender
        return self._senders[stream_id]

    def _make_stream_recipient(self, stream_id: int) -> HTTP2Recipient:
        """Recipient with consume-time WINDOW_UPDATE crediting.

        Why credit on consumption rather than on delivery:
        ``docs/about/rfc9113-implementation.md`` §6.9.1.
        """

        # Unannotated for the per-request-closure reason (see app.py::_wrap_send_native).
        async def _credit(n, sid=stream_id):
            # Skip the stream-level frame once the stream is released (§5.1
            # forbids non-PRIORITY frames on a closed stream); the
            # connection-level credit must still flow or stream-0 leaks shut.
            if sid in self._recipients:
                await self.send_frame(self.factory.window_update(sid, n))
            await self.send_frame(self.factory.window_update(0, n))

        return RecipientFactory.http2(
            queue_depth=self._stream_queue_depth,
            credit_callback=_credit,
            credit_budget=self._inbound_stream_window,
            max_body=self._max_body_size,
            min_rate=self._min_body_rate,
            min_rate_grace=self._min_body_rate_grace,
        )

    def _release_recipient_credit(self, recipient) -> None:
        """Replay a released stream's un-consumed inbound credit to stream 0.

        Stream-level credit is not replayed: the stream is closed (RFC 9113
        §5.1).  The connection-level half, and what leaks without it, is
        ``docs/about/rfc9113-implementation.md`` §6.9.1.
        """
        take = getattr(recipient, 'take_uncredited', None)
        balance = take() if take is not None else 0
        if balance <= 0 or self._goaway_sent:
            return

        # Unannotated for the per-request-closure reason (see app.py::_wrap_send_native).
        async def _replay():
            try:
                await self.send_frame(self.factory.window_update(0, balance))
            except Exception:
                if _DEBUG:
                    logger.debug('post-stream connection credit replay failed',
                                 exc_info=True)

        # Callers may be sync done-callbacks, so the replay is scheduled:
        # the connection TaskGroup where run() can await it, a bare loop task
        # once the group is closing.  Each create_task must get its OWN
        # coroutine object — CPython 3.13's TaskGroup.create_task() closes the
        # coroutine before raising, so reusing it here schedules an
        # already-closed one, the task dies with "cannot reuse already awaited
        # coroutine", and the credit is silently never replayed on exactly the
        # teardown path this fallback exists for.
        task = None
        if self._task_group is not None:
            try:
                task = self._task_group.create_task(_replay())
            except RuntimeError:
                task = None
        if task is None:
            coro = _replay()
            try:
                task = asyncio.get_running_loop().create_task(coro)
            except RuntimeError:
                coro.close()
                return  # no running loop — nothing left to credit against
        self._credit_flush_tasks.add(task)
        task.add_done_callback(self._credit_flush_tasks.discard)

    def _fill_scope_connection(self, conn: Connection) -> None:
        """Inject peername/sockname into a freshly-parsed HTTP/2 Connection."""
        if self._peername:
            conn.client = tuple(self._peername[:2])
        if self._sockname:
            conn.server = tuple(self._sockname[:2])

    @log
    async def send_frame(self, frame: FrameBase) -> None:
        """Send a raw HTTP/2 frame via the control-plane sender.

        Every emitted RST_STREAM passes through here, which is what makes
        this the one place the G8 blind spot can be closed without dusting
        the counter across a dozen refusal sites.
        """
        await self._control_sender(frame)
        if (frame.FrameType() == FrameTypes.RST_STREAM
                and not self._goaway_sent):
            await self._count_emitted_rst()

    def _validate_stream_state(
        self, stream: Stream, frame_type: FrameTypes,
    ) -> tuple[ErrorCodes, str] | None:
        """Return (error_code, level) if frame_type is not allowed in stream.state.

        Per RFC 9113 §5.1.  Returns ``None`` when the frame is allowed.
        ``level`` is ``'connection'`` or ``'stream'``.
        """
        state = stream.state

        if state == StreamState.IDLE:
            if frame_type in (FrameTypes.HEADERS, FrameTypes.PRIORITY,
                              FrameTypes.CONTINUATION, FrameTypes.PUSH_PROMISE):
                return None
            return (ErrorCodes.PROTOCOL_ERROR, 'connection')

        if state == StreamState.HALF_CLOSED_REMOTE:
            if frame_type in (FrameTypes.PRIORITY, FrameTypes.WINDOW_UPDATE,
                              FrameTypes.RST_STREAM):
                return None
            return (ErrorCodes.STREAM_CLOSED, 'stream')

        if state == StreamState.CLOSED:
            if frame_type == FrameTypes.PRIORITY:
                return None
            would_reopen_the_stream = frame_type in (
                FrameTypes.HEADERS, FrameTypes.CONTINUATION)
            if would_reopen_the_stream:
                return (ErrorCodes.STREAM_CLOSED, 'connection')
            # Anything else may be the peer racing our RST_STREAM / END_STREAM.
            return (ErrorCodes.STREAM_CLOSED, 'stream')

        return None

    async def _connection_error(
        self, error_code: 'ErrorCodes', reason: str = '',
    ) -> None:
        """Send GOAWAY with ``error_code``, half-close the writer, mark exit.

        The write half is closed rather than left open because h2spec's
        VerifyConnectionClose only succeeds on a real TCP close; the frame
        loop then drains on the next iteration via EOF.  Idempotent.
        """
        if self._goaway_sent:
            return
        logger.warning(
            'HTTP/2 connection error %s: %s', error_code.name, reason)
        await self.send_frame(
            self.factory.goaway(self._last_peer_stream_id, error_code))
        self._goaway_sent = True
        try:
            await self._writer.close()
        except Exception:
            # Already closed (peer hung up); we have done our part.
            if _DEBUG:
                logger.debug('writer.close raised on connection-error path',
                             exc_info=True)

    def _make_done_cb(
        self, stream_id: int, *, is_ws: bool = False,
    ) -> Callable[[asyncio.Task], None]:
        """Return a done-callback that releases per-stream resources on completion.

        ``is_ws`` is tagged at the call site rather than blanket-decremented:
        letting regular HTTP completions touch ``_ws_stream_count`` would
        drift it below the true in-flight count and the RFC 8441 cap would
        over-admit.
        """

        # Unannotated for the per-request-closure reason (see app.py::_wrap_send_native).
        def _cb(_task):
            self._active_stream_count = max(0, self._active_stream_count - 1)
            if is_ws:
                self._ws_stream_count = max(0, self._ws_stream_count - 1)
            self._stream_tasks.pop(stream_id, None)
            self._senders.pop(stream_id, None)
            released = self._recipients.pop(stream_id, None)
            if released is not None:
                self._release_recipient_credit(released)
            # Prune the node but remember the id, so late frames still reach
            # the CLOSED branch of §5.1 validation without this connection
            # paying a Stream object per completed request.
            if self.root_stream.children.pop(stream_id, None) is not None:
                self._mark_closed(stream_id, via_rst=False)
        return _cb

    def _mark_closed(self, stream_id: int, via_rst: bool) -> None:
        """Record *stream_id* as closed for §5.1 late-frame validation."""
        self._closed_streams[stream_id] = via_rst
        if stream_id > self._closed_high_water:
            self._closed_high_water = stream_id
        if len(self._closed_streams) > _CLOSED_STREAMS_CAP:
            # Evict the oldest recorded id (dict preserves insertion order).
            del self._closed_streams[next(iter(self._closed_streams))]

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
        from ..env import get_settings as _get_settings  # noqa: PLC0415
        cfg = _get_settings()

        # RFC 8441 §3 — never invite Extended CONNECT unless the operator has
        # opted in, because a peer that never sees the bit will not send it.
        await self.send_frame(self.factory.settings(
            enable_connect_protocol=cfg.h2_enable_websocket,
            initial_window_size=cfg.h2_initial_window_size,
            max_concurrent_streams=self.max_concurrent_streams,
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
        self._header_block_since = None

        async with asyncio.TaskGroup() as tg:
            self._task_group = tg
            watchdog = tg.create_task(self._liveness_watchdog())
            try:
                await self._frame_loop(tg)
            finally:
                watchdog.cancel()

        self._task_group = None

    async def _meter(self, window: RateWindow, what: str) -> bool:
        """Count one frame; close the connection if the budget is spent.

        Returns True when the caller should stop processing this frame —
        the connection is ending either way, but the frame loop's
        ``continue`` lets the peer read the GOAWAY before the close, which
        is the difference between a diagnosable refusal and a reset socket.
        """
        if not window.hit():
            return False
        log_cap_hit('frame_rate',
                    requested=window.count, limit=window.limit,
                    protocol='http2')
        await self._connection_error(
            ErrorCodes.ENHANCE_YOUR_CALM,
            f'{what} rate limit exceeded ({window.count} in {window.window}s)')
        return True

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
        await self._close_connection(ErrorCodes.ENHANCE_YOUR_CALM)

    async def _liveness_watchdog(self) -> None:
        """Bound how long a peer may take, without touching the frame read.

        ``BB_HEADER_TIMEOUT``, ``BB_H2_IDLE_TIMEOUT`` and
        ``BB_H2_PING_TIMEOUT`` (see ``docs/reference/env-vars.md``) share one
        loop because a timestamp answers all three.  It sleeps to the earliest
        deadline that applies and re-evaluates on waking, so an idle connection
        costs one wake-up per idle period rather than a fixed tick.

        All three end the connection by closing the *writer*: the frame loop is
        parked in a read, and closing the transport makes that read return EOF,
        a path the loop already handles.  Cancelling it would abandon a
        partially-read frame and leave teardown racing the stream tasks.
        """
        if self._h2_idle_timeout <= 0 and self._header_timeout <= 0:
            return
        loop = asyncio.get_running_loop()
        while True:
            now = loop.time()
            if self._probe_sent_at is not None:
                wake_at = self._probe_sent_at + self._h2_ping_timeout
                expired = 'probe'
            elif self._header_block_since is not None and self._header_timeout > 0:
                wake_at = self._header_block_since + self._header_timeout
                expired = 'header'
            elif self._h2_idle_timeout > 0:
                wake_at = self._last_frame_at + self._h2_idle_timeout
                expired = 'idle'
            else:
                # Nothing to watch until the frame loop opens a header block.
                await asyncio.sleep(self._header_timeout)
                continue

            if wake_at > now:
                await asyncio.sleep(wake_at - now)
                continue  # state may have changed while we slept

            if expired == 'header':
                await self._end_for_stalled_header_block()
                return
            if expired == 'probe':
                await self._end_for_unresponsive_peer()
                return
            await self._probe_peer()

    async def _probe_peer(self) -> None:
        """Ask a silent peer whether it is still there (RFC 9113 §6.7)."""
        self._probe_sent_at = asyncio.get_running_loop().time()
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
        await self._close_connection(ErrorCodes.ENHANCE_YOUR_CALM)

    async def _end_for_unresponsive_peer(self) -> None:
        """The probe went unanswered: the peer is gone, not merely quiet.

        ``NO_ERROR`` because nothing was violated — see
        ``docs/about/rfc9113-implementation.md`` §6.7.
        """
        logger.info('HTTP/2 peer did not answer the liveness PING in %.1fs '
                    '— GOAWAY', self._h2_ping_timeout)
        await self._close_connection(ErrorCodes.NO_ERROR)

    async def _close_connection(self, error_code: int) -> None:
        with contextlib.suppress(Exception):
            await self.send_frame(self.factory.goaway(
                self._last_peer_stream_id, error_code))
        self._goaway_sent = True
        with contextlib.suppress(Exception):
            result = self._writer.close()
            # Async on the asyncio adapter, sync on the raw transports; accept
            # both rather than make every caller know which it holds.
            if inspect.isawaitable(result):
                await result

    async def _frame_loop(self, tg: asyncio.TaskGroup) -> None:
        """Read frames and dispatch stream tasks until EOF or GOAWAY."""
        waiting_continuation = False
        header_frame = None
        _tasks_since_yield = 0
        _yield_every = self._frame_yield_every
        _loop = asyncio.get_running_loop()
        # Bound once: read on every inbound frame, and the attribute walk is
        # the avoidable half of the cost.  The clock read itself stays — it is
        # what makes ``BB_H2_IDLE_TIMEOUT`` mean the period it says, and a
        # stated time bound is not traded for a fraction of a microsecond.
        _loop_time = _loop.time

        while data := await self.receive():
            # ``loop.time()``, not ``time.monotonic()``: the watchdog sleeps on
            # the loop's clock, and two clocks that agree today are a bug
            # waiting for a loop implementation that reads a different one.
            self._last_frame_at = _loop_time()
            self._probe_sent_at = None
            if self._goaway_sent:
                # Signal before exiting: a stream task blocked in receive()
                # would never get http.disconnect, so the enclosing TaskGroup
                # would wait on it forever and the connection would wedge.
                _signal_recipients(self._recipients)
                return
            if self._oversize_frame_len:
                # The un-read payload stays in the socket buffer and is
                # discarded on close; the frame is never loaded.
                n = self._oversize_frame_len
                self._oversize_frame_len = 0
                await self._connection_error(
                    ErrorCodes.FRAME_SIZE_ERROR,
                    f'frame length {n} exceeds SETTINGS_MAX_FRAME_SIZE '
                    f'{DEFAULT_MAX_FRAME_SIZE}')
                _signal_recipients(self._recipients)
                return
            frame = self.factory.load(data)
            frame_type = frame.FrameType()

            # RFC 9113 §6.10 — inside a header block, unknown frame types
            # included.
            if waiting_continuation and frame_type != FrameTypes.CONTINUATION:
                name = frame_type.name if frame_type is not None else 'unknown'
                await self._connection_error(
                    ErrorCodes.PROTOCOL_ERROR,
                    f'expected CONTINUATION, got {name}')
                continue  # let h2spec read the GOAWAY before we close

            # RFC 9113 §5.5 — outside a header block, unknown types are ignored.
            if frame_type is None:
                continue

            # CVE-2023-44487 (Rapid Reset).  Metered before stream-state
            # validation, so abusive RSTs on idle or unknown streams count
            # toward the budget alongside legitimate ones.
            if frame_type == FrameTypes.RST_STREAM:
                if await self._meter(self._rst_meter, 'RST_STREAM'):
                    continue

            # CVE-2019-9512 / CVE-2019-9515.  Metered before the responder
            # runs, so the ACK is refused rather than merely counted.
            elif frame_type == FrameTypes.PING:
                if await self._meter(self._ping_meter, 'PING'):
                    continue
            elif frame_type == FrameTypes.SETTINGS:
                if await self._meter(self._settings_meter, 'SETTINGS'):
                    continue

            # CVE-2019-9518's shape.  A zero-length frame adds nothing to any
            # byte budget, so only a count can see it.
            if (frame.length == 0
                    and frame_type in (FrameTypes.CONTINUATION, FrameTypes.DATA)):
                if await self._meter(self._empty_frame_meter,
                                     f'empty {frame_type.name}'):
                    continue

            # RFC 9113 §4.2 — see _FRAME_SIZE_CONNECTION_ERROR_TYPES; a frame
            # on stream 0 is connection-fatal for the same reason.
            if frame.length > DEFAULT_MAX_FRAME_SIZE:
                if (frame_type in self._FRAME_SIZE_CONNECTION_ERROR_TYPES
                        or frame.stream_id == 0):
                    await self._connection_error(
                        ErrorCodes.FRAME_SIZE_ERROR,
                        f'{frame_type.name} length {frame.length} > '
                        f'{DEFAULT_MAX_FRAME_SIZE}')
                else:
                    await self.send_frame(self.factory.rst_stream(
                        frame.stream_id, ErrorCodes.FRAME_SIZE_ERROR))
                continue

            # RFC 9113 §6.1-6.4, §6.6, §6.10 — see _STREAM_ONLY_FRAME_TYPES.
            if frame.stream_id == 0 and frame_type in self._STREAM_ONLY_FRAME_TYPES:
                await self._connection_error(
                    ErrorCodes.PROTOCOL_ERROR,
                    f'{frame_type.name} with stream_id 0')
                continue

            # RFC 9113 §6.10, independent of stream state — and so it must
            # precede stream-state validation, or a stray CONTINUATION on a
            # half-closed stream gets STREAM_CLOSED instead.
            if frame_type == FrameTypes.CONTINUATION and not waiting_continuation:
                await self._connection_error(
                    ErrorCodes.PROTOCOL_ERROR,
                    'unexpected CONTINUATION without preceding HEADERS')
                continue

            # RFC 9113 §6.3 — PRIORITY payload MUST be 5 octets.
            if frame_type == FrameTypes.PRIORITY and frame.length != 5:
                await self.send_frame(self.factory.rst_stream(
                    frame.stream_id, ErrorCodes.FRAME_SIZE_ERROR))
                continue

            # Live streams sit directly under root; closed ones have been
            # pruned to _closed_streams, which is what still separates CLOSED
            # from IDLE below.
            stream = self.root_stream.children.get(frame.stream_id)

            if frame.stream_id != 0 and stream is None:
                closed_via_rst = self._closed_streams.get(frame.stream_id)
                is_closed = closed_via_rst is not None
                if not is_closed and 0 < frame.stream_id <= self._closed_high_water:
                    # Evicted from the bounded record — still CLOSED, not IDLE.
                    # Assume the lenient close, so a late WINDOW_UPDATE or
                    # RST_STREAM is ignored rather than answered.
                    is_closed = True
                    closed_via_rst = False
                if is_closed:
                    # Late frame on a CLOSED stream (§5.1).
                    if frame_type == FrameTypes.PRIORITY:
                        pass  # always allowed
                    elif frame_type in (FrameTypes.HEADERS, FrameTypes.CONTINUATION):
                        await self._connection_error(
                            ErrorCodes.STREAM_CLOSED,
                            f'{frame_type.name} on closed stream {frame.stream_id}')
                        continue
                    elif (not closed_via_rst and frame_type in (
                            FrameTypes.WINDOW_UPDATE, FrameTypes.RST_STREAM)):
                        # RFC 9113 §5.1 — on a stream *we* closed with
                        # END_STREAM, a WINDOW_UPDATE or RST_STREAM the peer
                        # sent before it saw that MUST be silently ignored,
                        # not answered: the client crediting our last response
                        # DATA races our trailers' END_STREAM, and an RST makes
                        # it tear the stream down early.
                        continue
                    else:
                        await self.send_frame(self.factory.rst_stream(
                            frame.stream_id, ErrorCodes.STREAM_CLOSED))
                        continue
                else:
                    if frame_type == FrameTypes.HEADERS:
                        # RFC 9113 §5.1.1 — odd, and strictly increasing.
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
                        # §6.3 lets a peer prioritise a stream it has not
                        # opened, so this is legal on an idle stream — and must
                        # create nothing, or the peer grows one node per frame
                        # that nothing will ever read (§5.3 deprecated the
                        # dependency scheme).  ``stream`` stays None;
                        # PriorityResponder still validates the frame.
                        pass
                    else:
                        await self._connection_error(
                            ErrorCodes.PROTOCOL_ERROR,
                            f'frame {frame_type.name} on idle stream '
                            f'{frame.stream_id}')
                        continue

            # RFC 9113 §5.1.  Stream 0 has no stream state to validate against.
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
                        # The peer now owes CONTINUATION; start its clock.
                        self._header_block_since = _loop.time()
                case FrameTypes.CONTINUATION:
                    send = self.make_sender(stream.stream_id)
                    spawned = await self._on_continuation_frame(
                        frame, stream, send, tg, header_frame, waiting_continuation)
                    if spawned:
                        waiting_continuation = False
                        header_frame = None
                        self._header_block_since = None
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
        conn: Connection,
        recipient,
        send,
        log_record,
    ) -> None:
        """Spawn the StreamActor that runs one stream's app dispatch.

        *conn* is always the native [`Connection`][]; the compat lane's ASGI
        scope is derived beyond here, at the app boundary, and nowhere else.

        A ``BB_REQUEST_TIMEOUT`` expiry sends RST_STREAM CANCEL and lets the
        task complete normally, so it does not cancel the TaskGroup.
        """
        self._active_stream_count += 1

        # One dispatch path, never a fork on the aggregator: ``StreamActor`` is
        # already None-tolerant in both fields that would differ, and a second
        # path would duplicate ``RequestActor``'s plumbing while losing this
        # one's failure handling — the part a peer can act on, since a raising
        # stream is reset with INTERNAL_ERROR rather than left silent.
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

        try:
            if self._stream_semaphore is not None:
                task = tg.create_task(
                    _run_when_stream_cap_admits(make_final, self._stream_semaphore))
            else:
                task = tg.create_task(make_final())
        except RuntimeError:
            # HEADERS arrived in the turn the connection went away; no peer
            # left to RST_STREAM.
            if _DEBUG:
                logger.debug('stream %d not started: connection closing',
                             stream_id)
            return

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

    async def _on_headers_frame(
        self,
        frame,
        stream: 'Stream',
        send,
        tg: asyncio.TaskGroup,
    ) -> bool:
        """Handle HEADERS; return False only while awaiting CONTINUATION."""
        if not frame.end_headers:
            return False

        return await self._complete_header_block(frame, stream, send, tg)

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
        if stream.conn is not None:
            # RFC 9113 §8.1 — a second field section is trailers, not a new
            # request.  They are not surfaced to the app, so only the
            # END_STREAM transition is observable.  Handling it here is what
            # makes single-frame and fragmented trailers share one recipient,
            # and what stops a second handler from starting.
            if not header_frame.end_stream:
                await self.send_frame(self.factory.rst_stream(
                    stream.stream_id, ErrorCodes.PROTOCOL_ERROR))
            else:
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

        if conn.type == 'websocket':
            # RFC 8441 Extended CONNECT.  Threaded natively even under
            # BB_FORCE_ASGI_SCOPE — the WS extras live on the Connection, and
            # that lane round-trips only the HTTP app boundary.  Reaching here
            # with the setting off means a non-conforming peer: we never
            # advertised ENABLE_CONNECT_PROTOCOL.
            stream.expected_content_length = _extract_content_length(conn)
            stream.conn = conn
            if not self._ws_over_h2_enabled:
                await self.send_frame(self.factory.rst_stream(
                    stream.stream_id, ErrorCodes.PROTOCOL_ERROR))
                return True
            stream.on_headers_received(end_stream=False)
            log_record = _start_record(conn)
            await self._handle_h2_websocket(stream, tg, log_record)
            return True

        stream.expected_content_length = _extract_content_length(conn)
        stream.conn = conn

        # Guarded inline rather than behind a predicate: a stream is a request,
        # and a method call to answer "no" measured 21 executed instructions
        # per request where this comparison costs seven.  The refusal
        # re-checks; it, not this, is where the limit is enforced.
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
        # ``open_record`` owns the gate and returns ``None`` when nothing will
        # read the record.  Capture is inline in the sender's own arms, which
        # guard on that None.  A wrapping ``send`` cannot do the job: the
        # dict-shaped wrapper never sees a NativeResponse, so status and bytes
        # regress to '-' and 0 — and it costs a per-event coroutine dispatch.
        log_record = _open_record(conn, self._aggregator)
        send._log_record = log_record
        self._spawn_stream_task(tg, stream.stream_id, conn, stream_recipient, send, log_record)
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
        """Handle CONTINUATION; return False only while still accumulating."""
        # RFC 9113 §6.10 — the frame loop's own check, reached when a caller
        # arrives without one.
        if not waiting_continuation or header_frame is None:
            await self._connection_error(
                ErrorCodes.PROTOCOL_ERROR,
                'unexpected CONTINUATION without preceding HEADERS')
            return True

        if frame.stream_id != header_frame.stream_id:
            await self._connection_error(
                ErrorCodes.PROTOCOL_ERROR,
                f'CONTINUATION stream {frame.stream_id} does not match '
                f'opening HEADERS stream {header_frame.stream_id}')
            return True

        # A bytearray so each CONTINUATION is an amortised-O(1) extend, not the
        # O(n²) ``bytes += bytes`` that reallocates the block per frame.
        # parse_payload() wraps raw_block in BytesIO, which accepts bytearray.
        if not isinstance(header_frame.raw_block, bytearray):
            header_frame.raw_block = bytearray(header_frame.raw_block)
        header_frame.raw_block += frame.payload

        # CONTINUATION flood / CVE-2024-27983 — capped before the block
        # reaches the HPACK decoder.  ENHANCE_YOUR_CALM is the standard code
        # for "header block too large" (RFC 6585 §5 / RFC 9113 §7).
        if len(header_frame.raw_block) > self._header_max_total:
            logger.warning(
                'Stream %d header block exceeded BB_HEADER_MAX_TOTAL=%d '
                'across CONTINUATION frames — RST_STREAM ENHANCE_YOUR_CALM',
                stream.stream_id, self._header_max_total,
            )
            log_cap_hit('header_max_total',
                        requested=len(header_frame.raw_block),
                        limit=self._header_max_total,
                        protocol='http2')
            await self.send_frame(self.factory.rst_stream(
                stream.stream_id, ErrorCodes.ENHANCE_YOUR_CALM))
            return True

        if not frame.end_headers:
            return False

        header_frame.parse_payload()
        return await self._complete_header_block(
            header_frame, stream, send, tg)

    async def _refuse_oversized_declared_body(self, stream, conn, send) -> bool:
        """413 the stream whose head declared more body than the cap allows.

        RFC 9113 §8.1 names the sequence: the complete response before the
        request finishes, then ``RST_STREAM(NO_ERROR)``.  Why the connection
        survives here where HTTP/1.1's refusal must close it:
        ``BB_MAX_BODY_SIZE`` in ``docs/reference/env-vars.md``.  A body with no
        ``content-length`` declares nothing to refuse; ``HTTP2Recipient``
        counts that one as DATA arrives.

        Callers gate on the same comparison inline, so the common answer costs
        no call.  This re-checks because it, not the gate, is where the limit
        is enforced: a caller that forgot the guard still gets it right.
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

        Regular request streams credit on *consumption* and do it from the
        recipient's own callback, so nothing is credited here; the enqueue-time
        branch below is only for recipients without one.  Both mechanics are
        RFC 9113 §6.9.1 (``docs/about/rfc9113-implementation.md``).
        """
        if stream.state in (StreamState.HALF_CLOSED_REMOTE, StreamState.CLOSED):
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
                await self.send_frame(self.factory.rst_stream(
                    stream.stream_id, ErrorCodes.PROTOCOL_ERROR))
                return

        stream.on_data_received(end_stream=bool(frame.end_stream))
        if stream.stream_id in self._recipients:
            recipient = self._recipients[stream.stream_id]
            delivered = recipient.put_DATAFrame(frame)
            if (delivered and frame.length
                    and not getattr(recipient, 'credits_on_consume', False)):
                # Enqueue-time credit, for recipients without a consume-time
                # callback: HTTP2WSReader under its buffer cap, push streams,
                # direct test constructions.  Both windows, per §6.9.1.
                #
                # The ``frame.length`` guard is load-bearing: a zero-length
                # DATA frame consumes no window, and a WINDOW_UPDATE of 0 is a
                # protocol error that a strict client drops the connection on.
                # grpcio sends exactly that frame to close a client-streaming
                # request.
                await self.send_frame(
                    self.factory.window_update(stream.stream_id, frame.length))
                await self.send_frame(
                    self.factory.window_update(0, frame.length))
            elif delivered:
                pass  # zero-length carrier, or the recipient credits on consume
            elif getattr(recipient, 'backpressures_via_credit', False):
                # Buffered, but withholding credit as back-pressure; the
                # recipient replays it once its buffer drains.  No RST_STREAM —
                # the bytes are safe.
                pass
            else:
                # Refused: an overrun of the advertised window, a degenerate
                # tiny-frame flood, or a body limit.  Tell the recipient as
                # well, so a handler parked in ``receive()`` for a body that
                # will never continue unwinds now rather than at the timeout.
                await self.send_frame(
                    self.factory.rst_stream(stream.stream_id, ErrorCodes.ENHANCE_YOUR_CALM))
                recipient.put_disconnect()
        else:
            logger.warning('DATA for stream %d but no recipient found', stream.stream_id)

    async def _on_goaway_frame(self, last_stream_id: int) -> None:
        """Handle an incoming GOAWAY: echo one back and signal all recipients.

        Echoing the peer's last_stream_id is how it learns what it may safely
        retry (RFC 9113 §6.8).
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
        from ..env import get_settings as _get_settings  # noqa: PLC0415
        from .conn_id import new_connection_id  # noqa: PLC0415
        from .websocket_actor import WebSocketActor  # noqa: PLC0415
        from .http2_ws import HTTP2WSReader, HTTP2WSWriter  # noqa: PLC0415

        # Without this per-connection cap a peer may hold
        # ``max_concurrent_streams`` idle WS streams.  ``0`` disables it.
        cfg = _get_settings()
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
        stream_send = self.make_sender(stream.stream_id)

        async def _ws_send_200(subprotocol=None):
            headers = []
            if subprotocol:
                sp = subprotocol if isinstance(subprotocol, str) else subprotocol.decode()
                headers = [(b'sec-websocket-protocol', sp.encode())]
            # Flushed now, not through http.response.start: HTTP2Sender
            # coalesces HEADERS with the first DATA, and an RFC 8441 accept has
            # no body, so the HEADERS would never leave and the handshake would
            # hang.  No END_STREAM — the stream stays open for WS DATA frames.
            await stream_send.send_response_headers(HTTPStatus(200), headers)

        conn._ws = {'send_101': _ws_send_200}

        sid = stream.stream_id

        # Unannotated for the per-request-closure reason (see app.py::_wrap_send_native).
        async def _replay_credit(n):
            # Both windows, so a reader that withheld credit at its buffer cap
            # reopens the peer's symmetrically with the per-frame path.
            await self.send_frame(self.factory.window_update(sid, n))
            await self.send_frame(self.factory.window_update(0, n))

        ws_reader = HTTP2WSReader(credit_callback=_replay_credit)
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

        self._ws_stream_count += 1
        self._active_stream_count += 1
        task = tg.create_task(_run_ws())
        task.add_done_callback(
            self._make_done_cb(stream.stream_id, is_ws=True))

    async def _handle_push(self, event: HTTPResponsePushEvent,
                           parent_stream_id: int) -> None:
        """Handle an 'http.response.push' ASGI event.

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

        from .parser import _split_h2_path  # noqa: PLC0415

        push_stream_id = self._allocate_push_stream_id()
        path = event.get('path', '/')

        parent_stream = self.root_stream.find_child(parent_stream_id)
        parent = parent_stream.conn if parent_stream is not None else None
        # Plain attribute reads, never ``.get()`` on a scope: under
        # BB_FORCE_ASGI_SCOPE that reaches a header *list* and raises
        # AttributeError for every push.  A parent can genuinely be missing —
        # pushed against an already-evicted stream — so the defaults are
        # spelled out rather than left to an empty-dict sentinel that would
        # turn a typo into a silent empty value.
        if parent is not None:
            parent_headers = parent.headers
            parent_scheme = parent.scheme
            _parent_client = parent.client
        else:
            parent_headers, parent_scheme, _parent_client = Headers([]), 'https', None
        # §8.3.1 maps ``:authority`` into ``host``, so a dispatched parent
        # always carries one; the fallback only covers a parent with none.
        raw_authority = (parent_headers.get(b'host') or b'localhost')
        authority = raw_authority.decode() if isinstance(raw_authority, bytes) else raw_authority

        from ..protocol.frame_types import PseudoHeaders  # noqa: PLC0415
        pseudo = {
            PseudoHeaders.METHOD:    'GET',
            PseudoHeaders.PATH:      path,
            PseudoHeaders.SCHEME:    parent_scheme,
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

        _pushed_path, _pushed_raw_path, _pushed_query = _split_h2_path(path)
        pushed_conn = Connection(
            method='GET',
            path=_pushed_path,
            raw_path=_pushed_raw_path,
            headers=Headers([(k.encode() if isinstance(k, str) else k,
                              v.encode() if isinstance(v, str) else v)
                             for k, v in regular]),
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
        log_record = _start_record(pushed_conn)
        push_sender._log_record = log_record
        capturing_send = push_sender

        if self._task_group is not None:
            self._spawn_stream_task(
                self._task_group, push_stream_id, pushed_conn,
                push_recipient, capturing_send, log_record,
            )

    async def _handle(self, msg: Message) -> None:
        raise NotImplementedError
