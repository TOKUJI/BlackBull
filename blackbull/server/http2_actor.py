"""HTTP/2 Actor classes for the BlackBull Actor model (Phase 6 Step 4).

HTTP2Actor drives the HTTP/2 connection state machine for one TCP connection.
StreamActor owns the lifetime of a single HTTP/2 stream.
"""
import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from http import HTTPStatus
from typing import Any, Protocol, runtime_checkable

from ..actor import Actor, Message
from ..event_aggregator import EventAggregator
from ..logger import log
from ..protocol.frame import FrameFactory
from ..protocol.frame_types import (
    ErrorCodes, FrameBase, FrameTypes,
    DEFAULT_INITIAL_WINDOW_SIZE, DEFAULT_MAX_FRAME_SIZE,
)
from ..protocol.stream import Stream, StreamState
from ..connection import Connection, CONNECTION_STASH_KEY, bind_receive_channel
from ..env import get_settings
from ..headers import Headers
from .parser import parse_headers
from .cap_log import log_cap_hit
from .recipient import (AbstractReader, IncompleteReadError,
                        HTTP2Recipient, RecipientFactory,
                        _HTTP2_STREAM_QUEUE_DEPTH)
from .response import ResponderFactory
from .sender import AbstractWriter, ConnectionWindow, SenderFactory
from .access_log import (
    AccessLogRecord, _make_capturing_send, _make_disconnect_detecting_receive,
    emit_access_log as _emit_access_log,
    request_record_needed as _request_record_needed,
    disconnect_events_observed as _disconnect_events_observed,
)
from ..asgi import ASGIEvent
from .http1_actor import RequestActor

logger = logging.getLogger(__name__)


@runtime_checkable
class _StreamRecipient(Protocol):
    """Duck-type interface shared by HTTP2Recipient and HTTP2WSReader."""
    def put_disconnect(self) -> None: pass
    def put_DATAFrame(self, frame) -> bool: pass


def _req_headers(target) -> Headers:
    """Request headers of a dispatch *target* — the native :class:`Connection`
    (attribute) or, on the ``force_asgi`` / WebSocket lanes, an ASGI scope dict.

    Always returns a :class:`Headers`.  A scope dict's ``headers`` is a plain
    ``list`` of ``(bytes, bytes)`` tuples (the ASGI wire shape), so it is wrapped
    here — callers rely on ``Headers`` methods (``_resolve_priority`` calls
    ``.get()``); iterating one (``_extract_content_length``) works either way.
    Wrapping happens only on the compat lane; the native path returns the
    ``Connection``'s ``Headers`` as-is."""
    if isinstance(target, Connection):
        return target.headers
    raw = target.get('headers', [])
    return raw if isinstance(raw, Headers) else Headers(raw)


def _extract_content_length(target) -> int | None:
    """Return the int value of the request's content-length, or None.

    Returns None when the header is absent OR when the value does not parse
    as a non-negative integer (parse_headers may have already rejected the
    request as malformed in the latter case).  RFC 9113 §8.1.2.6 covers the
    "must equal sum of DATA payloads" semantics enforced by the caller.
    """
    for name, value in _req_headers(target):
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


def _make_log_record(conn: Connection):
    # Publish the record for the app layer: BlackBull._dispatch sources the
    # request_completed detail's wire fields (status / response_bytes /
    # duration_ms) from the request's ``conn.state['access_log']`` (Sprint 64
    # event consolidation) — same contract as the HTTP/1.1 actor. The actor
    # always has the parsed Connection, even on the BB_FORCE_ASGI_SCOPE lane
    # (where the emitted scope shares ``conn.state`` by identity, so the app's
    # rebuilt Connection sees the same record) — so the log record is always
    # built from the Connection, never an ASGI scope dict.
    record = AccessLogRecord.from_conn(conn)
    conn.state['access_log'] = record
    return record

_DEFAULT_PRIORITY: dict[str, int | bool] = {'urgency': 3, 'incremental': False}

# Upper bound on the per-connection closed-stream record (bug 1.9).  Streams
# closed beyond this many back fall off the exact cache and are recognised as
# CLOSED via the high-water mark instead, defaulting to the lenient
# (non-RST-close) §5.1 treatment.
_CLOSED_STREAMS_CAP = 1024


def _build_h2_extensions(
    stream_id: int,
    priority: dict,
    peer_initial_window: int,
    connection_window: int,
) -> dict:
    """Return a freshly-built ``scope['extensions']`` dict for one HTTP/2 request.

    The shape Sprint 32 introduces:

    - ``http.response.push`` — empty marker; signals the application can
      send ``http.response.push`` events on this scope (existing behaviour).
    - ``http.response.priority`` — ``{'urgency': int, 'incremental': bool}``
      per RFC 9218 §4.1.  Field names match the gunicorn beta HTTP/2
      surface; the *contents* are RFC 9218 rather than the deprecated
      RFC 7540 weight/tree (RFC 9113 §5.3.2 deprecated the tree, and
      modern clients send RFC 9218 priority signals).
    - ``http.response.http2_stream`` — ``{'stream_id': int,
      'send_window_remaining': int, 'connection_send_window_remaining':
      int}``.  Snapshot at scope-build time; the windows shift as the
      response body streams.  Lays the foundation for gRPC server-streaming
      back-pressure awareness.

    Peer recv-window is intentionally absent: BlackBull sends
    WINDOW_UPDATE per consumed DATA frame, so there is no scalar to
    snapshot.
    """
    return {
        ASGIEvent.HTTP_RESPONSE_PUSH: {},
        'http.response.priority': priority,
        'http.response.http2_stream': {
            'stream_id': stream_id,
            'send_window_remaining': peer_initial_window,
            'connection_send_window_remaining': connection_window,
        },
    }




async def _run_guarded(coro, sem):
    # BB_H2_ACTIVE_STREAMS_1W semaphore: caps concurrently-running handlers on a
    # single-worker deployment.  Without it, one high-mux connection can saturate
    # the event loop with handler coroutines and starve connections handled by the
    # same worker.  Each acquire yields to the event loop, so stream tasks that
    # cannot enter still receive frames; they just don't start the ASGI app call.
    async with sem:
        await coro


# ---------------------------------------------------------------------------
# Level A message types (Actor inbox protocol)
# ---------------------------------------------------------------------------



# ---------------------------------------------------------------------------
# Priority helper (mirrors server.py; consolidated in a later step)
# ---------------------------------------------------------------------------

def _resolve_priority(stream: 'Stream', target) -> dict[str, int | bool]:
    if stream.priority_hint is not None:
        return stream.priority_hint
    raw = _req_headers(target).get(b'priority', b'')
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
        conn: 'dict | Connection',
        receive: Callable[..., Awaitable[Any]],
        send: Callable[..., Awaitable[Any]],
        app: Callable[..., Awaitable[None]],
        aggregator: EventAggregator,
        http2_actor: 'HTTP2Actor',
        log_record,
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

    async def run(self) -> None:
        try:
            await RequestActor(
                self._conn, self._receive, self._send,
                self._app, self._aggregator,
            ).run()
        except Exception:
            await self._http2_actor.send_frame(
                self._http2_actor.factory.rst_stream(
                    self._stream_id, ErrorCodes.INTERNAL_ERROR)
            )
        finally:
            # log_record is None on the baseline hot path when nothing consumes
            # it (no access log, no request_completed listener) — the gate lives
            # at the dispatch call sites (_request_record_needed).
            if self._log_record is not None:
                _emit_access_log(self._log_record)

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

    # CVE-2023-44487 (Rapid Reset) rolling-window thresholds.  20
    # RST_STREAMs per second is generous for legitimate clients
    # (browser navigation + prefetch cancellation rarely exceeds
    # ~10/s) and limiting for the attack shape (CVE-2023-44487
    # exploits routinely hit thousands/s).  Promote to env vars if a
    # real workload surfaces that legitimately exceeds the threshold.
    _RST_RATE_LIMIT: int = 20
    _RST_RATE_WINDOW: float = 1.0  # seconds

    # Frame types whose payload size violation is a connection error
    # rather than a stream error (RFC 9113 §4.2).  Pre-computed as a
    # class-level frozenset so _frame_loop avoids allocating a tuple on
    # every iteration.
    #
    # Extension compatibility: if a future RFC or extension introduces a
    # new frame type that can alter connection state (carries a header
    # block or targets stream 0), add it here.  The frozenset pattern is
    # the same shape as the previous inline tuple; the dispatch logic is
    # unchanged.
    _FRAME_SIZE_CONNECTION_ERROR_TYPES: frozenset[FrameTypes] = frozenset({
        FrameTypes.HEADERS,
        FrameTypes.CONTINUATION,
        FrameTypes.PUSH_PROMISE,
        FrameTypes.SETTINGS,
    })

    # Frame types that MUST NOT appear on stream 0.  Each receives a
    # connection error of type PROTOCOL_ERROR per the RFC sections below.
    # SETTINGS/PING/GOAWAY MUST be on stream 0 (inverse requirement);
    # WINDOW_UPDATE may be on stream 0 or non-zero (no restriction).
    #
    # Consolidates six independent checks in _frame_loop into one lookup.
    # Previously RST_STREAM (§6.4) and PUSH_PROMISE (§6.6) were missing
    # from the individual checks — now included for full RFC 9113 coverage.
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
        # Accept-time connection id (ConnectionActor's) — the one id for the
        # whole connection.  The RFC 8441 WS path reuses it in the stream
        # scope instead of minting a second one; empty when the actor is
        # constructed directly (tests), in which case that path falls back
        # to generating a fresh id.
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
        # RFC 9113 header-block hard ceiling — matches the HTTP/1.1
        # ``BB_HEADER_MAX_TOTAL`` budget so a CONTINUATION-flood peer
        # can't grow ``header_frame.raw_block`` without bound.  When
        # exceeded, the offending stream gets RST_STREAM
        # ENHANCE_YOUR_CALM (RFC 6585 §5 / RFC 9113 §7) — the same
        # code nginx and Envoy use for this condition.
        self._header_max_total: int = _cfg.header_max_total
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
        # Shared connection-level send window (bug 1.2).  Every stream sender
        # created by ``make_sender`` references this one object, so N concurrent
        # streams debit a single stream-0 budget instead of N private copies.
        self._conn_window = ConnectionWindow(DEFAULT_INITIAL_WINDOW_SIZE)
        # The per-stream inbound window we advertise in SETTINGS (run() sends
        # SETTINGS_INITIAL_WINDOW_SIZE from the same settings object).  Doubles
        # as each stream recipient's byte budget under consume-based inbound
        # crediting: a conformant peer can never have more un-credited bytes
        # in flight than this.
        self._inbound_stream_window: int = _cfg.h2_initial_window_size
        # Fire-and-forget WINDOW_UPDATE(0) replay tasks created when a stream
        # is released with an un-consumed credit balance (see
        # _release_recipient_credit).  Held so the loop's weak refs can't drop
        # a pending replay.
        self._credit_flush_tasks: set[asyncio.Task] = set()

        # Concurrent-stream counter — incremented when a stream task is spawned,
        # decremented via Task.add_done_callback when the task finishes.
        self._active_stream_count: int = 0

        # Per-stream handler tasks, keyed by stream_id.  Kept so an inbound
        # RST_STREAM (client cancellation) can cancel the running handler:
        # otherwise a server-streaming handler abandoned mid-flight blocks
        # forever in the sender's flow-control wait (the departed client never
        # sends WINDOW_UPDATE), permanently holding a max_concurrent_streams
        # slot.  Under a high-churn streaming client that leaks slots until new
        # streams are REFUSED_STREAM'd — the "streaming collapse" the bench hit.
        self._stream_tasks: dict[int, asyncio.Task] = {}

        # Per-stream recipients, keyed by stream_id.  Stored on the actor so
        # _make_done_cb can remove entries when streams complete.
        self._recipients: dict[int, _StreamRecipient] = {}

        # Set when we have sent a GOAWAY for a connection-level protocol error.
        # The frame loop exits cleanly on the next iteration, giving the GOAWAY
        # time to flush before the connection closes.
        self._goaway_sent: bool = False

        # RFC 9113 §5.1.1 — peer-initiated stream IDs must strictly increase.
        self._last_peer_stream_id: int = 0

        # RFC 8441 — set by run() from BB_H2_ENABLE_WEBSOCKET.  When False,
        # any incoming :method=CONNECT with :protocol=websocket is refused.
        self._ws_over_h2_enabled: bool = False

        # Native dispatch by default (thread the Connection); set True by run()
        # only under BB_FORCE_ASGI_SCOPE. Default here for tests that drive the
        # frame handlers without calling run().
        self._force_asgi: bool = False

        # CVE-2023-44487 (Rapid Reset) guard — rolling RST_STREAM rate.
        # Attackers open a stream with HEADERS and immediately RST it,
        # churning the per-stream allocations (Stream node, sender,
        # recipient, HPACK context) without hitting
        # ``max_concurrent_streams`` because the lifecycle is too fast
        # for the counter to accumulate.  When the inbound RST_STREAM
        # rate exceeds the threshold, the connection is closed with
        # GOAWAY ENHANCE_YOUR_CALM.  Cheap counter, no allocation per
        # RST; threshold/window are class constants today and can be
        # promoted to env vars if a real workload surfaces that
        # legitimately exceeds them.
        self._rst_count: int = 0
        self._rst_window_start: float = 0.0

        # RFC 8441 stream-exhaustion guard — count of in-flight WebSocket
        # streams on this connection, capped at
        # ``cfg.h2_ws_max_streams_per_connection``.  Incremented in
        # :meth:`_handle_h2_websocket` before spawning the actor task;
        # decremented in the task's ``finally`` block when the WS handler
        # exits (normal close, app raise, or TaskGroup cancellation).
        self._ws_stream_count: int = 0

        # RFC 9113 §5.1 — closed-stream state, separate from the priority tree.
        # Maps stream_id → True if closed via RST_STREAM, False if closed via
        # END_STREAM / local completion.  This lets us drop closed nodes from
        # root_stream.children (keeping that dict O(active-streams) and so
        # find_child stays O(1)) while still recognizing late frames on
        # already-closed identifiers and choosing the right error code.
        # Bounded record of recently-closed stream ids → closed-via-RST bool,
        # for §5.1 late-frame validation.  Capped so a long-lived connection
        # cycling millions of streams (gRPC) does not grow it without bound
        # (bug 1.9).  Ids below _closed_high_water that have fallen off the cap
        # are still recognised as CLOSED via the watermark.
        self._closed_streams: dict[int, bool] = {}
        self._closed_high_water: int = 0
        # Set by receive() when a frame declares a payload larger than the
        # SETTINGS_MAX_FRAME_SIZE we advertise; the frame loop raises a
        # FRAME_SIZE_ERROR without the payload ever being buffered (bug 1.14).
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
                conn_window=self._conn_window,  # shared stream-0 budget (bug 1.2)
                # Seed the per-stream window from what the peer has currently
                # granted us, rather than the RFC default, which may already
                # have been changed by SETTINGS frames received before this
                # stream opened.  The connection window is shared, not copied.
                initial_window=self._peer_initial_window_size,
            )
            self._senders[stream_id] = sender
        return self._senders[stream_id]

    def _make_stream_recipient(self, stream_id: int) -> HTTP2Recipient:
        """Recipient with consume-time WINDOW_UPDATE crediting.

        Consume-based inbound flow control (the Sprint 62 deferral,
        ``proposals/consume-based-inbound-flow-control.md``): credit is
        replayed when the app pops the event off the queue, not when the
        DATA frame is enqueued, so a stalled handler closes the window and
        back-pressures the peer instead of overflowing the recipient queue
        into RST_STREAM(ENHANCE_YOUR_CALM).
        """
        async def _credit(n: int, sid: int = stream_id) -> None:
            # Mirrors the per-frame enqueue-credit shape (and the WS reader's
            # _replay_credit): stream + connection WINDOW_UPDATE, in that
            # order.  Skip the stream-level frame once the stream is released
            # (§5.1 forbids non-PRIORITY frames on a closed stream); the
            # connection-level credit must still flow or stream-0 leaks shut.
            if sid in self._recipients:
                await self.send_frame(self.factory.window_update(sid, n))
            await self.send_frame(self.factory.window_update(0, n))

        return RecipientFactory.http2(
            queue_depth=self._stream_queue_depth,
            credit_callback=_credit,
            credit_budget=self._inbound_stream_window,
        )

    def _release_recipient_credit(self, recipient) -> None:
        """Replay a released stream's un-consumed inbound credit to stream 0.

        Under consume-based crediting a handler that finished (or was RST)
        without draining its request body never credited those DATA bytes
        back.  The peer debited them from the shared CONNECTION window, which
        would leak shut for every later stream on this connection — so the
        balance is replayed as WINDOW_UPDATE(0) here.  Stream-level credit is
        not replayed: the stream is closed (RFC 9113 §5.1).
        """
        take = getattr(recipient, 'take_uncredited', None)
        balance = take() if take is not None else 0
        if balance <= 0 or self._goaway_sent:
            return

        async def _replay() -> None:
            try:
                await self.send_frame(self.factory.window_update(0, balance))
            except Exception:
                logger.debug('post-stream connection credit replay failed',
                             exc_info=True)

        # Callers may be sync done-callbacks — schedule the replay.  Prefer
        # the connection TaskGroup so run() awaits it; fall back to a bare
        # loop task when the group is already closing (teardown).
        coro = _replay()
        try:
            if self._task_group is not None:
                task = self._task_group.create_task(coro)
            else:
                task = asyncio.get_running_loop().create_task(coro)
        except RuntimeError:
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

    def _conn_to_asgi_scope(self, conn: Connection) -> dict:
        """Materialize an ASGI scope dict for the **compat lanes only**.

        The native HTTP/2 dispatch path never calls this — it threads the
        :class:`Connection` itself (WebSocket included, Sprint 80). The one
        boundary that still needs an ASGI scope is **``BB_FORCE_ASGI_SCOPE``**
        (§4.3): a pure ASGI scope round-tripped back through ``from_scope`` at
        the app boundary. Also used to synthesize the pushed-request scope for
        server push.
        """
        return (conn.to_asgi_scope(force_asgi=True) if self._force_asgi
                else conn.to_asgi_scope())

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

    def _make_done_cb(
        self, stream_id: int, *, is_ws: bool = False,
    ) -> Callable[[asyncio.Task], None]:
        """Return a done-callback that releases per-stream resources on completion.

        Keeps the Stream node in the tree (marked CLOSED) so that the
        frame-loop state validation can detect late frames arriving on the
        same identifier and respond with the appropriate STREAM_CLOSED
        error (RFC 9113 §5.1).

        ``is_ws=True`` additionally decrements ``_ws_stream_count`` —
        the RFC 8441 per-connection cap.  Tagged at the call site rather
        than blanket-decremented so regular HTTP stream completions
        don't silently drift the WS counter below the true in-flight
        count (which would cause the WS cap to over-admit).
        """
        def _cb(_task: asyncio.Task) -> None:
            self._active_stream_count = max(0, self._active_stream_count - 1)
            if is_ws:
                self._ws_stream_count = max(0, self._ws_stream_count - 1)
            self._stream_tasks.pop(stream_id, None)
            self._senders.pop(stream_id, None)
            released = self._recipients.pop(stream_id, None)
            if released is not None:
                # Consume-based crediting: a handler that never drained its
                # body leaves un-credited DATA bytes debited from the shared
                # connection window — replay the balance to stream 0.
                self._release_recipient_credit(released)
            # Prune the stream node from the tree and remember it as closed-
            # via-END_STREAM (closed_via_rst=False) so late frames hit the
            # CLOSED branch of §5.1 validation without keeping a Stream
            # object around for every completed request.
            if self.root_stream.children.pop(stream_id, None) is not None:
                self._mark_closed(stream_id, via_rst=False)
        return _cb

    def _mark_closed(self, stream_id: int, via_rst: bool) -> None:
        """Record *stream_id* as closed for §5.1 late-frame validation.

        Bounded (bug 1.9): only the most recent ``_CLOSED_STREAMS_CAP`` streams
        keep their exact closed-via-RST bool; older ids are evicted (oldest
        first) but stay recognisable as CLOSED through ``_closed_high_water``.
        """
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
            # RFC 9113 §4.2 (bug 1.14) — a frame larger than the
            # SETTINGS_MAX_FRAME_SIZE we advertise is a FRAME_SIZE_ERROR.
            # Refuse to buffer its (attacker-declared, up to 16 MiB) payload:
            # hand back the 9-byte header and let the frame loop raise the
            # connection error and close.
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

        # RFC 8441 §3 — only advertise SETTINGS_ENABLE_CONNECT_PROTOCOL when
        # the operator has opted in (BB_H2_ENABLE_WEBSOCKET=1).  Without the
        # bit set, peers MUST NOT send :protocol pseudo-headers or use
        # Extended CONNECT, so disabling it is the safe default.
        await self.send_frame(self.factory.settings(
            enable_connect_protocol=cfg.h2_enable_websocket,
            initial_window_size=cfg.h2_initial_window_size,
            max_concurrent_streams=self.max_concurrent_streams,
        ))
        self._ws_over_h2_enabled = cfg.h2_enable_websocket
        # Native HTTP/2 dispatch threads the Connection itself; only the
        # BB_FORCE_ASGI_SCOPE compat lane emits an ASGI scope dict (§4.3).
        self._force_asgi = cfg.force_asgi_scope
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
                # has been flushed.  Signal recipients before exiting (bug 1.8):
                # stream tasks blocked in receive() awaiting body would
                # otherwise never get http.disconnect, so the enclosing
                # TaskGroup waits on them forever and the connection wedges.
                # The normal EOF exit path already signals; this one didn't.
                _signal_recipients(self._recipients)
                return
            if self._oversize_frame_len:
                # receive() refused to buffer an over-sized frame's payload
                # (bug 1.14).  Raise the FRAME_SIZE_ERROR and close without
                # ever loading the frame — the un-read payload bytes stay in
                # the socket buffer and are discarded on close.
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

            # CVE-2023-44487 (Rapid Reset) — bound the per-second
            # inbound RST_STREAM rate before any per-stream work.
            # The attack opens a stream with HEADERS and immediately
            # RSTs it; ``max_concurrent_streams`` never catches it
            # because the lifecycle is too short to accumulate.
            # GOAWAY ENHANCE_YOUR_CALM closes the connection; a fresh
            # handshake is required to retry.  Placed BEFORE stream-
            # state validation so legitimate RSTs on a real open
            # stream and abusive RSTs on idle/unknown streams both
            # count toward the rolling budget.
            if frame_type == FrameTypes.RST_STREAM:
                now = time.monotonic()
                if now - self._rst_window_start > self._RST_RATE_WINDOW:
                    self._rst_count = 0
                    self._rst_window_start = now
                self._rst_count += 1
                if self._rst_count > self._RST_RATE_LIMIT:
                    await self._connection_error(
                        ErrorCodes.ENHANCE_YOUR_CALM,
                        f'RST_STREAM rate limit exceeded '
                        f'({self._rst_count} in '
                        f'{self._RST_RATE_WINDOW}s)')
                    continue

            # RFC 9113 §4.2 — a frame whose payload exceeds the receiver's
            # advertised SETTINGS_MAX_FRAME_SIZE is a FRAME_SIZE_ERROR.  When
            # the frame could alter connection state (carries a header block,
            # is SETTINGS, or targets stream 0), it is a connection error;
            # otherwise it is a stream error.
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

            # RFC 9113 §6.1-6.4, §6.6, §6.10 — DATA, HEADERS, PRIORITY,
            # RST_STREAM, PUSH_PROMISE, and CONTINUATION frames MUST be
            # associated with a non-zero stream identifier.  Violation is a
            # connection error of type PROTOCOL_ERROR in every case.
            # SETTINGS/PING/GOAWAY MUST be on stream 0 (inverse requirement);
            # WINDOW_UPDATE may be on stream 0 or non-zero.
            #
            # Consolidated from six independent checks into one frozenset
            # lookup.  RST_STREAM (§6.4) and PUSH_PROMISE (§6.6) were
            # previously missing from the individual checks; now covered.
            if frame.stream_id == 0 and frame_type in self._STREAM_ONLY_FRAME_TYPES:
                await self._connection_error(
                    ErrorCodes.PROTOCOL_ERROR,
                    f'{frame_type.name} with stream_id 0')
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
                closed_via_rst = self._closed_streams.get(frame.stream_id)
                is_closed = closed_via_rst is not None
                if not is_closed and 0 < frame.stream_id <= self._closed_high_water:
                    # Recorded as closed but evicted from the bounded cache
                    # (bug 1.9) — still CLOSED, not IDLE.  Default to the lenient
                    # (non-RST) treatment so a late WINDOW_UPDATE/RST_STREAM is
                    # ignored rather than answered.
                    is_closed = True
                    closed_via_rst = False
                if is_closed:
                    # Late frame on a CLOSED stream (§5.1).
                    if frame_type == FrameTypes.PRIORITY:
                        pass  # PRIORITY is always allowed; let it through
                    elif frame_type in (FrameTypes.HEADERS, FrameTypes.CONTINUATION):
                        await self._connection_error(
                            ErrorCodes.STREAM_CLOSED,
                            f'{frame_type.name} on closed stream {frame.stream_id}')
                        continue
                    elif (not closed_via_rst and frame_type in (
                            FrameTypes.WINDOW_UPDATE, FrameTypes.RST_STREAM)):
                        # RFC 9113 §5.1 — for a stream we closed by sending
                        # END_STREAM, a WINDOW_UPDATE or RST_STREAM the peer
                        # emitted before it processed our END_STREAM MUST be
                        # silently ignored, NOT answered with RST_STREAM.
                        # (e.g. the client crediting the last response DATA it
                        # received races our trailers' END_STREAM.)  Replying
                        # with RST makes the client tear the stream down early.
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
                    if stream.conn is not None and frame.end_headers:
                        # RFC 9113 §8.1 — a second (single-frame) HEADERS on an
                        # already-open request stream is *trailers*, not a new
                        # request.  Previously _validate_stream_state permitted
                        # HEADERS in OPEN and this respawned a second request
                        # over the live recipient/task (bug 1.14).  We don't
                        # surface request trailers to the ASGI app, so we mark a
                        # clean end-of-stream instead.  Trailers MUST carry
                        # END_STREAM; without it the request is malformed.
                        if not frame.end_stream:
                            await self.send_frame(self.factory.rst_stream(
                                stream.stream_id, ErrorCodes.PROTOCOL_ERROR))
                        else:
                            stream.on_data_received(end_stream=True)
                            recipient = self._recipients.get(stream.stream_id)
                            if recipient is not None:
                                recipient.put_event({
                                    'type': ASGIEvent.HTTP_REQUEST,
                                    'body': b'', 'more_body': False})
                    else:
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
        conn: 'dict | Connection',
        recipient,
        send,
        log_record,
    ) -> None:
        """Spawn a StreamActor (aggregator path) or legacy _run_with_log task.

        *conn* is the dispatch target the handler receives — the native
        :class:`Connection` on the HTTP path, or an ASGI scope dict on the
        WebSocket / ``force_asgi`` compat lanes.

        Increments ``_active_stream_count`` and registers ``_on_stream_done``
        so the counter is decremented when the task finishes.

        When ``BB_REQUEST_TIMEOUT > 0``, the stream coroutine is wrapped with
        ``asyncio.wait_for``; on expiry RST_STREAM CANCEL is sent and the task
        completes normally (does not cancel the TaskGroup).
        """
        self._active_stream_count += 1

        # Bind the *raw* recipient onto the Connection for lazy ``conn.body()``
        # before the disconnect-detecting wrapper is built, so ``conn._receive``
        # never captures ``conn`` through the wrapper (per-request cycle → cyclic
        # GC = v0.60.0 tail-latency regression). Idempotent (binds when unset).
        bind_receive_channel(conn, recipient)

        if self._aggregator is not None:
            # Wrap receive for disconnect detection only when a listener observes
            # it (request_disconnected / request_completed); otherwise dispatch the
            # raw recipient and save the per-request closure. Body-level disconnect
            # (conn.body() → ClientDisconnected) is independent of this wrapper.
            if _disconnect_events_observed(self._aggregator):
                dispatch_receive = _make_disconnect_detecting_receive(
                    recipient, conn, self._aggregator)
            else:
                dispatch_receive = recipient
            stream_actor = StreamActor(
                stream_id=stream_id,
                conn=conn,
                receive=dispatch_receive,
                send=send,
                app=self.app,
                aggregator=self._aggregator,
                http2_actor=self,
                log_record=log_record,
            )
            coro = stream_actor.run()
        else:
            from .server import _run_with_log  # noqa: PLC0415
            coro = _run_with_log(
                self.app(conn, recipient, send),
                log_record,
            )

        timeout = self._request_timeout
        if timeout > 0:
            _sp = conn.path if isinstance(conn, Connection) else conn.get('path')

            async def _timed(c=coro, sid=stream_id, t=timeout, sp=_sp):
                try:
                    await asyncio.wait_for(c, timeout=t)
                except asyncio.TimeoutError:
                    logger.warning(
                        'Stream %d timed out after %.1fs — RST_STREAM CANCEL', sid, t)
                    log_cap_hit('request_timeout',
                                requested=t, limit=t,
                                scope_path=sp, protocol='http2')
                    await self.send_frame(self.factory.rst_stream(sid, ErrorCodes.CANCEL))
            final_coro = _timed()
        else:
            final_coro = coro

        if self._stream_semaphore is not None:
            task = tg.create_task(_run_guarded(final_coro, self._stream_semaphore))
        else:
            task = tg.create_task(final_coro)

        self._stream_tasks[stream_id] = task
        task.add_done_callback(self._make_done_cb(stream_id))

    def _apply_priority_and_extensions(self, stream: 'Stream', target) -> None:
        """Resolve stream priority and attach the H/2 request extensions.

        Shared by the HEADERS and CONTINUATION completion paths. On the native
        path *target* is the :class:`Connection` the handler receives, so the H/2
        extensions go straight onto ``conn.extensions`` (read as
        ``conn.extensions['http.response.priority']``). On the ``force_asgi`` /
        WebSocket lanes *target* is an ASGI scope dict; there the deprecation
        alias ``scope['http2_priority']`` is also populated (one release).
        """
        priority = _resolve_priority(stream, target)
        extensions = _build_h2_extensions(
            stream.stream_id, priority,
            self._peer_initial_window_size, self._conn_window.size)
        if isinstance(target, Connection):
            target.extensions = extensions
        else:
            target['http2_priority'] = priority
            target['extensions'] = extensions

    async def _on_headers_frame(
        self,
        frame,
        stream: 'Stream',
        send,
        tg: asyncio.TaskGroup,
    ) -> bool:
        """Handle a HEADERS frame; return True if stream task spawned, False if awaiting CONTINUATION."""
        if self._active_stream_count >= self.max_concurrent_streams:
            if not frame.end_headers:
                # Bug 1.14 #2 — the header block legally continues in
                # CONTINUATION frames (RFC 9113 §6.10); refusing here would
                # leave run() not expecting them and escalate the peer's
                # legal CONTINUATION into a bogus connection error.  Keep
                # accumulating the block (the flood cap still bounds it);
                # ``_on_continuation_frame`` re-checks capacity at
                # END_HEADERS and refuses there — after the HPACK decode,
                # which must happen regardless to keep the dynamic table
                # in sync (§4.3).
                return False
            log_cap_hit('h2_max_concurrent_streams',
                        requested=self._active_stream_count + 1,
                        limit=self.max_concurrent_streams,
                        protocol='http2')
            await self.send_frame(
                self.factory.rst_stream(stream.stream_id, ErrorCodes.REFUSED_STREAM))
            return True  # refused — do not queue as waiting for CONTINUATION

        if not frame.end_headers:
            return False

        conn = parse_headers(frame)
        # RFC 9113 §8.1.1 / §8.2.1 — malformed HEADERS must be rejected with
        # a stream error of type PROTOCOL_ERROR rather than dispatched.
        # parse_payload sets the flag for field-level violations; parse_headers
        # sets it for missing/empty required pseudo-headers.
        if getattr(frame, 'malformed', False):
            logger.debug('Stream %d malformed HEADERS — %s',
                         stream.stream_id, frame.malformed_reason)
            await self.send_frame(
                self.factory.rst_stream(stream.stream_id, ErrorCodes.PROTOCOL_ERROR))
            return True

        assert conn is not None  # not malformed → parse_headers built a Connection
        self._fill_scope_connection(conn)

        if conn.type == 'websocket':
            # RFC 8441 — Extended CONNECT bootstrapping WebSocket over HTTP/2.
            # WebSocket is native too (Sprint 80): thread the Connection — its
            # WS extras (subprotocols / the deferred 200 responder) live on it,
            # so there is no scope dict even under BB_FORCE_ASGI_SCOPE (the
            # force-asgi lane only round-trips the HTTP app boundary).
            # Off by default (BB_H2_ENABLE_WEBSOCKET): when the operator has
            # not opted in we never advertised ENABLE_CONNECT_PROTOCOL, so a
            # conforming peer would not send :protocol=websocket.  A
            # non-conforming one is rejected here with PROTOCOL_ERROR.
            stream.expected_content_length = _extract_content_length(conn)
            stream.conn = conn
            if not self._ws_over_h2_enabled:
                await self.send_frame(self.factory.rst_stream(
                    stream.stream_id, ErrorCodes.PROTOCOL_ERROR))
                return True
            stream.on_headers_received(end_stream=False)
            log_record = _make_log_record(conn)
            await self._handle_h2_websocket(stream, tg, log_record)
            return True

        # Native HTTP dispatch: thread the Connection itself — no ASGI scope dict,
        # no ``from_scope`` rebuild at the app boundary. The BB_FORCE_ASGI_SCOPE
        # compat lane still emits a pure ASGI scope (§4.3).
        target = self._conn_to_asgi_scope(conn) if self._force_asgi else conn
        stream.expected_content_length = _extract_content_length(target)
        stream.conn = target

        self._apply_priority_and_extensions(stream, target)
        stream_recipient = self._make_stream_recipient(stream.stream_id)
        self._recipients[stream.stream_id] = stream_recipient
        stream.on_headers_received(end_stream=bool(frame.end_stream))
        if frame.end_stream:
            # No body to deliver — skip queue allocation; recipient synthesizes
            # the empty http.request event on first receive() call if needed.
            stream_recipient.mark_end_of_stream_on_headers()
        # Build the access-log record (and wrap send to capture status/bytes)
        # only when something consumes it: access log, phase trace, or a
        # request_completed listener. The legacy (aggregator=None) path always
        # builds it — _run_with_log emits unconditionally. See P3 (HTTP/1.1).
        if self._aggregator is None or _request_record_needed(self._aggregator):
            log_record = _make_log_record(conn)
            dispatch_send = _make_capturing_send(send, log_record)
        else:
            log_record = None
            dispatch_send = send
        self._spawn_stream_task(tg, stream.stream_id, target, stream_recipient, dispatch_send, log_record)
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

        # copy-reduction-http2 P3 — accumulate into a bytearray so each
        # CONTINUATION is an amortised-O(1) in-place extend rather than the
        # O(n²) ``bytes += bytes`` that reallocates the whole block per frame.
        # parse_payload() wraps raw_block in BytesIO, which accepts bytearray.
        if not isinstance(header_frame.raw_block, bytearray):
            header_frame.raw_block = bytearray(header_frame.raw_block)
        header_frame.raw_block += frame.payload

        # CVE-class CONTINUATION flood — an attacker that opens a
        # stream with END_HEADERS=0 and follows with unbounded
        # CONTINUATION frames at DEFAULT_MAX_FRAME_SIZE each would
        # otherwise grow ``raw_block`` until OOM.  Mirror the H/1.1
        # 64 KiB cap (BB_HEADER_MAX_TOTAL).  ENHANCE_YOUR_CALM is the
        # standard error code for "header block too large" (RFC 6585
        # §5 / RFC 9113 §7); nginx and Envoy use the same.
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

        conn = parse_headers(header_frame)
        # RFC 9113 §8.1.1 / §8.2.1 — same malformed-HEADERS check as the direct
        # HEADERS path; reject with RST_STREAM PROTOCOL_ERROR.
        if getattr(header_frame, 'malformed', False):
            logger.debug('Stream %d malformed HEADERS (via CONTINUATION) — %s',
                         stream.stream_id, header_frame.malformed_reason)
            await self.send_frame(
                self.factory.rst_stream(stream.stream_id, ErrorCodes.PROTOCOL_ERROR))
            return True

        assert conn is not None  # not malformed → parse_headers built a Connection
        self._fill_scope_connection(conn)
        # Native dispatch (Connection); ASGI scope only for the force_asgi compat
        # lane. The CONTINUATION path has no dedicated WS handler, so a WS request
        # split across CONTINUATION frames threads its Connection through the
        # generic dispatch (``__call__`` routes it by ``conn.type == 'websocket'``)
        # rather than through ``_handle_h2_websocket``.
        target = self._conn_to_asgi_scope(conn) if self._force_asgi else conn
        stream.expected_content_length = _extract_content_length(target)

        if self._active_stream_count >= self.max_concurrent_streams:
            log_cap_hit('h2_max_concurrent_streams',
                        requested=self._active_stream_count + 1,
                        limit=self.max_concurrent_streams,
                        scope_path=(target.path if isinstance(target, Connection)
                                    else target.get('path')),
                        protocol='http2')
            await self.send_frame(
                self.factory.rst_stream(stream.stream_id, ErrorCodes.REFUSED_STREAM))
            return True

        self._apply_priority_and_extensions(stream, target)
        stream.conn = target
        stream_recipient = self._make_stream_recipient(stream.stream_id)
        self._recipients[stream.stream_id] = stream_recipient
        # Same consumer-gate as the HEADERS path (P3): skip the record + capturing
        # send wrapper when nothing reads them.
        if self._aggregator is None or _request_record_needed(self._aggregator):
            log_record = _make_log_record(conn)
            dispatch_send = _make_capturing_send(send, log_record)
        else:
            log_record = None
            dispatch_send = send
        self._spawn_stream_task(tg, stream.stream_id, target, stream_recipient, dispatch_send, log_record)
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
            recipient = self._recipients[stream.stream_id]
            delivered = recipient.put_DATAFrame(frame)
            if (delivered and frame.length
                    and not getattr(recipient, 'credits_on_consume', False)):
                # Enqueue-time credit — only for recipients WITHOUT a
                # consume-time credit callback (HTTP2WSReader under its
                # buffer cap, push streams, direct test constructions).
                # Regular request streams credit at consume-time instead:
                # the recipient replays WINDOW_UPDATE when the app pops the
                # event (consume-based inbound flow control), so a stalled
                # handler closes the window and back-pressures the peer.
                #
                # RFC 9113 §6.9.1 — DATA frames are subject to BOTH
                # stream and connection-level flow control.  Crediting
                # only the stream window leaves the connection-level
                # window depleting toward zero across requests, which
                # stalls any subsequent body once it hits 65,535 bytes
                # cumulative.  Credit both.
                #
                # Guard on ``frame.length``: a zero-length DATA frame (e.g. the
                # empty END_STREAM frame grpcio sends to close a client-streaming
                # request) consumes no flow-control window, and a WINDOW_UPDATE
                # with a 0 increment is a protocol error (§6.9.1) — a strict
                # client (grpcio) drops the connection on it.  Client-streaming
                # made this reachable; unary rarely sends a zero-length DATA.
                await self.send_frame(
                    self.factory.window_update(stream.stream_id, frame.length))
                await self.send_frame(
                    self.factory.window_update(0, frame.length))
            elif delivered:
                # Either a zero-length DATA (END_STREAM carrier — no credit
                # owed) or a consume-crediting recipient (credit replayed by
                # its callback when the app pops the event; nothing owed at
                # enqueue).
                pass
            elif getattr(recipient, 'backpressures_via_credit', False):
                # Recipient buffered the bytes but signalled backpressure
                # by withholding credit (HTTP2WSReader past its buffer
                # cap).  The peer's stream window debits by frame.length
                # and the recipient replays the credit through its own
                # callback once readexactly drains below the cap.  No
                # RST_STREAM — the bytes are safe in the buffer.
                pass
            else:
                # True abuse backstop under consume-based crediting: the peer
                # either overran the advertised inbound window it was never
                # credited for, or dribbled a degenerate tiny-frame flood.  A
                # conformant peer is back-pressured by the closing window and
                # never reaches this.
                await self.send_frame(
                    self.factory.rst_stream(stream.stream_id, ErrorCodes.ENHANCE_YOUR_CALM))
        else:
            logger.warning('DATA for stream %d but no recipient found', stream.stream_id)

    async def _on_goaway_frame(self, last_stream_id: int) -> None:
        """Handle an incoming GOAWAY: echo one back and signal all recipients.

        RFC 9113 §6.8 — when a GOAWAY is received the endpoint SHOULD send
        its own GOAWAY before closing the connection so the peer knows
        which streams were processed.  We mirror the peer's last_stream_id
        back in our GOAWAY and then inject ``http.disconnect`` into every
        active stream recipient before returning from ``_frame_loop``.
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

        Stores a deferred _ws_send_200 callback in the Connection's WS bag
        (``conn._ws['send_101']``) that WebSocketActor._send() reads, so
        WebSocketActor is shared with the H/1.1 path unchanged.  The 200 HEADERS
        response (and optional sec-websocket-protocol) is sent when the app
        calls websocket.accept.
        """
        from ..env import get_settings as _get_settings  # noqa: PLC0415
        from .conn_id import new_connection_id  # noqa: PLC0415
        from .websocket_actor import WebSocketActor  # noqa: PLC0415
        from .http2_ws import HTTP2WSReader, HTTP2WSWriter  # noqa: PLC0415

        # RFC 8441 stream-exhaustion guard — without a per-connection cap
        # an attacker can hold up to ``max_concurrent_streams`` idle WS
        # streams per connection.  ``0`` disables (legacy / opt-out).
        cfg = _get_settings()
        ws_cap = cfg.h2_ws_max_streams_per_connection
        if ws_cap > 0 and self._ws_stream_count >= ws_cap:
            _ws_conn = stream.conn
            log_cap_hit('h2_ws_max_streams_per_connection',
                        requested=self._ws_stream_count + 1,
                        limit=ws_cap,
                        scope_path=_ws_conn.path if isinstance(_ws_conn, Connection) else None,
                        protocol='h2-ws')
            await self.send_frame(self.factory.rst_stream(
                stream.stream_id, ErrorCodes.REFUSED_STREAM))
            return

        conn = stream.conn  # WS streams store the native Connection here
        assert conn is not None
        # One id per TCP connection: reuse the accept-time id; mint one only
        # when the actor was constructed without it (direct test drives).
        conn.connection_id = self._connection_id or new_connection_id()
        stream_send = self.make_sender(stream.stream_id)

        async def _ws_send_200(subprotocol=None):
            headers = []
            if subprotocol:
                sp = subprotocol if isinstance(subprotocol, str) else subprotocol.decode()
                headers = [(b'sec-websocket-protocol', sp.encode())]
            # Flush the :status 200 HEADERS immediately.  The plain
            # http.response.start event would be buffered until a body event
            # (HTTP2Sender coalesces HEADERS + first DATA), but an RFC 8441
            # accept has no body — the HEADERS would never reach the client and
            # the handshake would hang.  send_response_headers writes them now
            # without END_STREAM so the stream stays open for WS DATA frames.
            await stream_send.send_response_headers(HTTPStatus(200), headers)

        # WebSocketActor calls this on websocket.accept (shared 'send_101' key).
        conn._ws = {'send_101': _ws_send_200}

        sid = stream.stream_id

        async def _replay_credit(n: int) -> None:
            # HTTP2WSReader hit its buffer cap and withheld credit while
            # delivering DATA frames; once readexactly drained below the
            # cap, replay both the stream-level and connection-level
            # WINDOW_UPDATE so the peer's window reopens symmetrically
            # with the regular per-frame path.
            await self.send_frame(self.factory.window_update(sid, n))
            await self.send_frame(self.factory.window_update(0, n))

        ws_reader = HTTP2WSReader(credit_callback=_replay_credit)
        ws_writer = HTTP2WSWriter(stream_send)
        self._recipients[stream.stream_id] = ws_reader  # DATA frames routed here

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
                # ``_ws_stream_count`` is decremented by
                # ``_make_done_cb(is_ws=True)`` below, sharing the
                # single lifecycle hook with ``_active_stream_count`` and
                # per-stream dicts.
                log_record.close_code = ws_actor._disconnect_code
                _emit_access_log(log_record)

        self._ws_stream_count += 1
        self._active_stream_count += 1
        task = tg.create_task(_run_ws())
        task.add_done_callback(
            self._make_done_cb(stream.stream_id, is_ws=True))

    async def _handle_push(self, event: dict, parent_stream_id: int) -> None:
        """Handle an 'http.response.push' ASGI event.

        RFC 9113 §8.4 (server push) — the pushed request MUST be safe,
        cacheable, and have no request body (§8.4.1); ``:method`` is always
        ``GET``.  The PUSH_PROMISE frame itself is §6.6.  RFC 9113 §8.3.1:
        the ``:path`` pseudo-header carries both path and query string; we
        split them with the same ``_split_h2_path`` as request HEADERS so
        the synthetic ASGI scope gets the same decoded ``path`` /
        ``raw_path`` / ``query_string`` contract.
        """
        from .parser import _split_h2_path  # noqa: PLC0415

        push_stream_id = self._allocate_push_stream_id()
        path = event.get('path', '/')

        parent_stream = self.root_stream.find_child(parent_stream_id)
        parent = (parent_stream.conn
                  if (parent_stream and parent_stream.conn is not None) else {})
        # The parent is the native Connection (HTTP) or an ASGI scope dict (WS /
        # force_asgi lane); read the shared request fields from either.
        parent_headers = (parent.headers if isinstance(parent, Connection)
                          else parent.get('headers', Headers([])))
        parent_scheme = (parent.scheme if isinstance(parent, Connection)
                         else parent.get('scheme', 'https'))
        _parent_client = (parent.client if isinstance(parent, Connection)
                          else parent.get('client'))
        # F.1b maps ``:authority`` into the request's ``host`` header, so any
        # dispatched parent stream carries one; ``localhost`` only covers a
        # parent with none.
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

        # ASGI: the decoded path component (no query) vs the raw query bytes.
        # RFC 9113 §8.3.1 puts both into the ``:path`` pseudo-header; split here.
        _pushed_path, _pushed_raw_path, _pushed_query = _split_h2_path(path)
        # Build the synthetic pushed request as a native Connection and dispatch
        # it natively (like the HEADERS path). The H/2 extensions go straight on
        # ``conn.extensions``; the ``force_asgi`` lane converts to an ASGI scope
        # (adding the ``http2_priority`` deprecation alias) at the boundary.
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
                self._conn_window.size),
        )
        push_target: 'dict | Connection' = pushed_conn
        if self._force_asgi:
            push_target = self._conn_to_asgi_scope(pushed_conn)
            push_target['http2_priority'] = _DEFAULT_PRIORITY  # deprecation alias

        push_recipient = RecipientFactory.http2(queue_depth=self._stream_queue_depth)
        # Pushed requests have no body — same lazy-queue path as GETs with END_STREAM on HEADERS.
        push_recipient.mark_end_of_stream_on_headers()
        self._recipients[push_stream_id] = push_recipient
        push_sender = SenderFactory.http2(
            self._writer, self.factory, push_stream_id, push_callback=None,
            conn_window=self._conn_window)
        log_record = _make_log_record(pushed_conn)
        capturing_send = _make_capturing_send(push_sender, log_record)

        if self._task_group is not None:
            self._spawn_stream_task(
                self._task_group, push_stream_id, push_target,
                push_recipient, capturing_send, log_record,
            )

    async def _handle(self, msg: Message) -> None:  # never reached
        raise NotImplementedError
