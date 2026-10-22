"""Protocol receive channels.

Native next_chunk returns bytes or None; ASGI __call__ returns events.
Both share completion state. HTTP/1.1 after_dispatch determines whether to
reuse, drain or close; never reuse a framing-invalid stream.
"""
import asyncio
import contextlib
from abc import ABC, abstractmethod
from collections import deque
from time import monotonic as _monotonic
from typing import Awaitable, Callable, NoReturn, Optional

from ..protocol.http1 import (ChunkedDecoder, FieldsTooLarge, IncompleteReadError,
                              MessageError)
from .cap_log import log_cap_hit
from . import deadline as _deadline
from .deadline import Deadline, Guard
from .sender import AbstractWriter, AsyncioWriter
from .ws_codec import (
    FramePayloadTooLarge, InvalidFrameLength, MessageTooLarge, WSOpcode,
    encode_frame, read_frame_header, read_payload,
)
from .constants import WSCloseCode
from .rate_window import ByteRateFloor, RateWindow
from ..asgi import ASGIEvent
from ..env import get_settings
from ..connection import Connection, disconnected, mark_disconnected
from ..request import ClientDisconnected
from ..protocol.frame_types import FrameBase, Data, DEFAULT_INITIAL_WINDOW_SIZE
from ..event import Event, EventDispatcher
import logging

logger = logging.getLogger(__name__)

_READ_LOOP_JOIN_TIMEOUT = 5.0

# Defaults behind BB_STREAM_QUEUE_DEPTH / BB_WS_QUEUE_DEPTH (see
# docs/reference/env-vars.md); ``WebSocketRecipient`` documents the two modes.
_HTTP2_STREAM_QUEUE_DEPTH = 64
_WS_EVENT_QUEUE_DEPTH = 256     # a deferred reader's depth when the knob is 0
_WS_READ_INLINE = 0

CONNECTION_REUSABLE = 0
CONNECTION_NEEDS_DRAIN = 1
CONNECTION_MUST_CLOSE = 2

# Frame-COUNT total for a consume-credited stream queue, whose byte total is the
# advertised inbound window.  A conformant peer may legally burst that window as
# 1-byte frames, so the count sits far above it; what it refuses is the
# zero/tiny-frame flood (CVE-2019-9518) no byte budget can see.
_EVENT_CAP_MULTIPLIER = 16

# WebSocket end sentinel; its close code is stored in _terminal_code.
_WS_CLOSED = object()


def _ws_disconnect(code: int | None):
    from ..websocket import WebSocketDisconnect  # noqa: PLC0415
    return WebSocketDisconnect(code or WSCloseCode.ABNORMAL)

_H2_DISCONNECT = object()       # as ``_WS_CLOSED``, for the H2 queue

# RFC 6455 §5.5 control opcodes.
_WS_CONTROL_OPS = (WSOpcode.CLOSE, WSOpcode.PING, WSOpcode.PONG)


# ---------------------------------------------------------------------------
# Reader abstraction
# ---------------------------------------------------------------------------

def _bad_request(detail: str):
    from http import HTTPStatus  # noqa: PLC0415
    from ..router import HTTPException  # noqa: PLC0415
    return HTTPException(HTTPStatus.BAD_REQUEST, detail)


def _content_too_large(detail: str):
    from http import HTTPStatus  # noqa: PLC0415
    from ..router import HTTPException  # noqa: PLC0415
    return HTTPException(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, detail)


# MAL-CHUNK-EXT-64K (CVE-2023-39326 class) — the bound on one chunk-size or
# trailer line, the BB_HEADER_MAX_LINE default: nothing legitimate needs more.
_CHUNK_LINE_MAX = 8192


class ProtocolError(Exception):
    """Raised when a WebSocket protocol violation is detected (RFC 6455).

    ``close_code`` is the RFC 6455 §7.4 status code that should appear in
    the CLOSE frame sent to the peer.  Defaults to 1002 (PROTOCOL_ERROR);
    UTF-8 violations use 1007.
    """
    def __init__(self, message: str, close_code: int = 1002):
        super().__init__(message)
        self.close_code = close_code


def _is_valid_close_code(code: int) -> bool:
    """RFC 6455 §7.4 — which close codes may appear on the wire.

    Allowed: 1000–1011 (defined), 3000–4999 (registered + private use).
    Disallowed even though numerically in 1000-range: 1004 (reserved),
    1005 (no status), 1006 (abnormal — TCP-only marker), 1015 (TLS-only
    marker).  1012-1014 are defined but accepting them is fine.
    """
    if code in (1004, 1005, 1006, 1015):
        return False
    if 1000 <= code <= 1015:
        return True
    if 3000 <= code <= 4999:
        return True
    return False


def _parse_close_payload(payload: bytes) -> tuple[int, bool]:
    """Decode a CLOSE frame payload.

    Returns ``(code, ok)`` where ``ok`` is False when the payload violates
    RFC 6455 §5.5.1 — length 1 (truncated code), disallowed code value, or
    non-UTF-8 reason text.  Empty payload is permitted and maps to code
    1000 (NORMAL).
    """
    if not payload:
        return 1000, True
    if len(payload) == 1:
        return 1002, False
    code = int.from_bytes(payload[:2], 'big', signed=False)
    if not _is_valid_close_code(code):
        return 1002, False
    if len(payload) > 2:
        try:
            payload[2:].decode('utf-8')
        except UnicodeDecodeError:
            return 1002, False
    return code, True


class AbstractReader(ABC):
    """Protocol-agnostic async byte-source.

    Mirrors ``AbstractWriter`` on the receive side.  Implementations wrap a
    concrete transport so that ``BaseRecipient`` subclasses stay runtime-agnostic.
    """

    @abstractmethod
    async def read(self, n: int) -> bytes: pass

    def has_buffered(self) -> bool:
        """True when bytes are already buffered, so a read need not block.

        The WebSocket control-frame watchdog probes this to decide whether it
        can service a frame without blocking the caller's path.  Default
        ``False`` — a reader that cannot report buffering is treated as
        "nothing available", which only disables proactive servicing, never
        correctness (control frames are still serviced at the next read).
        """
        return False

    def buffered_len(self) -> int:
        """Bytes currently buffered, unconsumed.  Default 0."""
        return 0

    def peek(self, n: int) -> bytes:
        """Up to *n* buffered bytes without consuming them."""
        raise NotImplementedError(f'{type(self).__name__} cannot peek')

    async def fill(self, n: int) -> bool:
        """Buffer *n* bytes for [`peek`][]; return False on early EOF."""
        raise NotImplementedError(f'{type(self).__name__} cannot peek')

    def at_eof(self) -> bool:
        """Return True once the peer has closed and the buffer is drained.

        Default ``False`` (callers that need EOF detection — e.g. a long-lived
        raw-protocol read loop — should use a reader that overrides this).
        """
        return False

    async def readuntil(self, sep: bytes, limit: int = 0) -> bytes:
        """Read through *sep*; *limit* > 0 bounds it ([`ReadLimitExceeded`][])."""
        raise NotImplementedError(f'{type(self).__name__} cannot read lines')

    async def readexactly(self, n: int) -> bytes:
        """Read exactly *n* bytes.  Default: accumulate via ``read``.
        Concrete transport readers override this."""
        buf = bytearray()
        while len(buf) < n:
            chunk = await self.read(n - len(buf))
            if not chunk:
                break
            buf += chunk
        return bytes(buf)

    async def read_head(self, limit: int) -> bytes:
        """One message head — start line, field lines, terminator included.

        Three outcomes, because the caller answers each differently:

        * a complete head → returned;
        * EOF before a single byte of it → ``b''``, an idle close;
        * EOF part-way through → [`IncompleteReadError`][] carrying the
          partial, which is a truncated request and not an idle close.

        *limit* bounds the whole head and is what stops an unbounded read; 0
        disables it, and an overrun raises [`ReadLimitExceeded`][].

        The Internals page argues why this is a contract every reader meets
        rather than a capability the caller sniffs for.
        """
        raise NotImplementedError(f'{type(self).__name__} cannot read lines')


class FragmentAssembler:
    """Assemble data frames into (opcode, payload, compressed), or None until complete.

    Reject continuation without an opener, a new opener during assembly, and RSV1
    on continuations. max_total=0 disables the pre-append size cap; breaches raise
    MessageTooLarge for CLOSE 1009. This cap bounds compressed bytes; inflation
    requires its own bound.
    """

    def __init__(self, max_total: int = 0) -> None:
        self._max_total = max_total
        self._opcode: int | None = None
        self._buf: bytearray | None = None
        # RSV1 of the message-opener frame (RFC 7692: only the first frame of a
        # compressed message carries RSV1=1; continuations keep it clear).
        self._compressed: bool = False

    @property
    def in_progress(self) -> bool:
        return self._opcode is not None

    def feed(self, opcode: int, payload: bytes, fin: bool, rsv1: bool = False
             ) -> tuple[int, bytes, bool] | None:
        """Feed one frame; return ``(message_opcode, full_payload, compressed)`` on completion, else ``None``."""
        if opcode == WSOpcode.CONTINUATION:
            if not self.in_progress:
                raise ProtocolError(
                    'CONTINUATION frame received with no fragmentation in progress'
                )
            if rsv1:
                # RFC 7692 §6: RSV1 MUST be clear on continuation frames.
                raise ProtocolError(
                    'CONTINUATION frame with RSV1 set is a protocol violation'
                )
            assert self._buf is not None
            assert self._opcode is not None
            if self._max_total and len(self._buf) + len(payload) > self._max_total:
                raise MessageTooLarge(len(self._buf) + len(payload),
                                      self._max_total)
            self._buf += payload
            if fin:
                result = (self._opcode, bytes(self._buf), self._compressed)
                self._opcode = None
                self._buf = None
                self._compressed = False
                return result
            return None
        else:
            if self.in_progress:
                raise ProtocolError(
                    'New data frame received while a fragmented message is in progress'
                )
            if fin:
                return (opcode, payload, rsv1)
            # The opener is bounded too — the continuation check runs on the
            # *next* frame, so without this the frame cap would stand in for the
            # message total (security-model.md §The invariant).
            if self._max_total and len(payload) > self._max_total:
                raise MessageTooLarge(len(payload), self._max_total)
            self._opcode = opcode
            self._buf = bytearray(payload)
            self._compressed = rsv1
            return None


# ---------------------------------------------------------------------------
# Recipient hierarchy
# ---------------------------------------------------------------------------

class BaseRecipient(ABC):
    """Abstract base for ASGI-event receive callables.

    ``__call__`` returns an ASGI event dict appropriate to the protocol:
      - HTTP: ``{'type': 'http.request', 'body': ..., 'more_body': False}``
      - WebSocket: ``{'type': 'websocket.connect'}``,
                   ``{'type': 'websocket.receive', ...}``, or
                   ``{'type': 'websocket.disconnect', ...}``

    The actual byte transport is hidden behind ``AbstractReader`` so the
    recipient logic is decoupled from asyncio internals.
    """

    def __init__(self, reader: AbstractReader | None):
        self._reader = reader

    @abstractmethod
    async def __call__(self) -> dict: pass


class HTTP1Recipient(BaseRecipient):
    """Yield lazy HTTP/1.1 request-body slices as http.request events.

    *framing* is ``request_framing(conn.headers)``, already validated; the body
    is read by it, not by the headers again. After the terminal body event,
    subsequent calls return http.disconnect.
    """

    _reader: AbstractReader  # narrows BaseRecipient._reader from AbstractReader | None

    def __init__(self, reader: AbstractReader, conn: Connection,
                 *, body_timeout: float = 0.0,
                 deadline: Guard | None = None,
                 chunk_size: int | None = None,
                 chunk_max: int | None = None,
                 max_body: int | None = None,
                 min_rate: float | None = None,
                 min_rate_grace: float | None = None,
                 framing: tuple[int | None, bool]):
        super().__init__(reader)
        # ``chunk_size`` slices a chunked body, ``chunk_max`` bounds one
        # transport-paced Content-Length read; see docs/reference/env-vars.md.
        # All five fall back to settings when a caller does not inject them.
        if (chunk_size is None or chunk_max is None or max_body is None
                or min_rate is None or min_rate_grace is None):
            _s = get_settings()
            if chunk_size is None:
                chunk_size = _s.body_chunk_size
            if chunk_max is None:
                chunk_max = _s.body_chunk_max
            if max_body is None:
                max_body = _s.max_body_size
            if min_rate is None:
                min_rate = _s.min_body_rate
            if min_rate_grace is None:
                min_rate_grace = _s.min_body_rate_grace
        self._chunk_size = chunk_size
        # ``BB_MAX_BODY_SIZE``, counted on the octets rather than on the
        # declaration, so a directly-driven recipient or an external ASGI host
        # gets the same ceiling the actor applies at head time.
        self._max_body = max_body
        # ``BB_MIN_BODY_RATE`` / ``BB_MIN_BODY_RATE_GRACE`` — the anti-trickle
        # floor a per-read deadline cannot express; see env-vars.md.
        self._min_rate = min_rate
        self._min_rate_grace = min_rate_grace
        self._chunk_max = max(chunk_max, 1)
        # ``BB_BODY_TIMEOUT``, applied per ``_read_with_timeout`` call — which is
        # per *slice* on both framings; see env-vars.md.
        self._body_timeout = body_timeout
        self._deadline = deadline
        self.bind(conn, framing)

    def bind(self, conn: Connection,
             framing: tuple[int | None, bool]) -> 'HTTP1Recipient':
        """Point this recipient at *conn*, the next request on the connection,
        whose body framing is *framing*: ``(Content-Length or None, chunked)``
        as ``http1_actor.request_framing`` returned it.

        The reader, chunk size, and deadline are properties of the connection
        and survive; the per-request state starts over.  One
        recipient per connection is safe only because HTTP/1.1 dispatches one
        request at a time — **not** for HTTP/2, whose streams are concurrent.

        The split is the whole contract: any per-request field left out of this
        method would leak from request N into request N+1, so new state belongs
        here, not in ``__init__``.
        """
        # Do not retain conn: it binds this recipient as _receive.
        # Keep only the path for diagnostics to avoid a request reference cycle.
        self._req_path: str | None = conn.path
        self._content_length, self._chunked = framing
        # Per request: an inherited half-read chunk would splice request N's
        # body into request N+1.
        self._decoder = None
        self._done = False
        self._body_seen = 0
        # Transport-wait seconds only, never the handler's own — the delivery
        # rate's denominator (env-vars.md, ``BB_MIN_BODY_RATE_GRACE``).
        self._body_wait = 0.0
        self._judged_wait = 0.0
        self._rate_floor = None         # made on the first body chunk
        # Over the size cap or below the rate floor; see [`must_close`][].
        self._body_refused = False
        # A chunked-framing violation; see [`must_close`][].
        self.framing_broken = False
        return self

    @property
    def must_close(self) -> bool:
        """This connection cannot carry another request.

        Two causes, one consequence.  A chunked-framing violation leaves the
        byte stream desynced; a body refused for size leaves octets we
        deliberately did not read.  Either way the bytes that follow are the
        peer's to choose, and parsing them as the next request line is the
        request-smuggling shape — so the answer is to close, not to resynchronise.
        """
        return self.framing_broken or self._body_refused

    def needs_drain(self) -> bool:
        """True if a declared request body may still be buffered unread.

        A handler that ignores ``receive`` (e.g. a 404/405 response to a POST)
        leaves the body bytes in the reader; the next keep-alive request would
        then parse them as its request line.  A body-less request (GET, no
        Content-Length, not chunked) never needs draining.

        Kept as the named question it is, for tests and for a directly-driven
        recipient; the actor asks [`after_dispatch`][] instead, which answers
        this and ``must_close`` in one call.
        """
        if self.framing_broken or self._body_refused:
            # Draining a refused body would read the very octets the refusal
            # declined, and re-raise the 413 on the way.
            return False
        return not self._done and (self._chunked or bool(self._content_length))

    def after_dispatch(self) -> int:
        """What the connection should do now the handler has answered.

        One of ``CONNECTION_REUSABLE``, ``CONNECTION_NEEDS_DRAIN`` (an unread
        body stands between here and the next request), or
        ``CONNECTION_MUST_CLOSE``.
        """
        if self.framing_broken or self._body_refused:
            return CONNECTION_MUST_CLOSE
        if not self._done and (self._chunked or bool(self._content_length)):
            return CONNECTION_NEEDS_DRAIN
        return CONNECTION_REUSABLE

    async def drain(self, max_bytes: int) -> bool:
        """Discard any unread request body so the next pipelined request parses
        cleanly.  Returns True if fully drained (or the peer disconnected),
        False if the unread body exceeded *max_bytes* — the caller should then
        close the connection rather than keep it alive.
        """
        # Lazily, once per drain rather than per chunk: ``router`` cannot be
        # imported at module scope here (see [`_bad_request`][]).
        from ..router import HTTPException  # noqa: PLC0415
        drained = 0
        while not self._done:
            try:
                chunk = await self.next_chunk()
            except ClientDisconnected:
                # EOF or body_timeout mid-drain: nothing left to desync.
                return True
            except HTTPException:
                # A body limit tripped while draining: reachable only for an
                # *undeclared* over-cap body the handler never read.  Report
                # "could not drain" — the caller closes, which is what a refused
                # body asks for — rather than let a 413 with no request left to
                # answer reach the connection's generic error handler.
                self._body_refused = True
                return False
            if chunk is None:
                break
            drained += len(chunk)
            if drained > max_bytes:
                return False
        return True

    async def _next_chunked(self) -> bytes | None:
        decoder = self._decoder
        if decoder is None:
            decoder = self._decoder = ChunkedDecoder(
                self._reader, self._read_with_timeout, line_max=_CHUNK_LINE_MAX,
                slice_max=self._chunk_size)
        try:
            data = await decoder.next()
        except FieldsTooLarge as exc:
            self.framing_broken = True
            log_cap_hit('h1_chunk_line_length', requested=exc.length,
                        limit=_CHUNK_LINE_MAX, scope_path=self._req_path,
                        protocol='http1')
            raise _bad_request('chunk framing line exceeds length limit') from None
        except MessageError as exc:
            # The stream is desynced: the actor closes rather than keep-alive.
            self.framing_broken = True
            raise _bad_request(str(exc)) from None
        if data is None:
            self._done = True
            return None
        return self._account(data)

    def _account(self, chunk: bytes) -> bytes:
        """Weigh *chunk* against the two body limits, giving up if it fails one.

        Every delivered octet passes through here on both framings, so the
        limits belong to the recipient rather than to whichever caller drives it.
        Both verdicts are permanent for the connection ([`must_close`][]).

        They answer differently, and that is the point: too large is a judgement
        about the *request*, which the peer is entitled to hear — 413.  Too slow
        is a judgement about the *peer*, and answering it politely would be
        answering the attack, so the connection is abandoned as ``body_timeout``
        abandons a silent one.
        """
        self._body_seen += len(chunk)
        if self._max_body and self._body_seen > self._max_body:
            self._body_refused = True
            self._done = True
            log_cap_hit('max_body_size',
                        requested=self._body_seen, limit=self._max_body,
                        scope_path=self._req_path, protocol='http1')
            raise _content_too_large(
                f'request body exceeds {self._max_body} bytes')
        if self._min_rate:
            floor = self._rate_floor
            if floor is None:
                floor = self._rate_floor = ByteRateFloor(self._min_rate, self._min_rate_grace)
            waited, self._judged_wait = self._body_wait - self._judged_wait, self._body_wait
            if floor.record(len(chunk), waited):
                self._body_refused = True
                self._done = True
                log_cap_hit('min_body_rate', requested=floor.observed,
                            limit=self._min_rate,
                            scope_path=self._req_path, protocol='http1')
                raise ClientDisconnected()
        return chunk

    async def _read_with_timeout(self, coro, _payload: bool = False):
        """Run *coro* under the configured body_timeout, if any.

        Also the one place body reads wait, which is why the rate detector's
        clock lives here: the elapsed time it accumulates is transport-wait
        time only, never the handler's own.
        """
        # ``None``, not 0.0, for "not timing": a clock reading is a value, not a
        # flag, and 0.0 is one a monotonic clock is allowed to return.
        t0 = _monotonic() if self._min_rate else None
        try:
            if self._body_timeout > 0 and self._deadline is not None:
                with self._deadline.within(self._body_timeout):
                    return await coro
            if self._body_timeout > 0:
                # Fallback for a caller with no Guard; same
                # per-call semantics, but a fresh Timeout per chunk.
                return await asyncio.wait_for(coro, timeout=self._body_timeout)
            return await coro
        finally:
            if t0 is not None:
                self._body_wait += _monotonic() - t0

    async def next_chunk(self) -> bytes | None:
        """The next body chunk, or ``None`` once the body is complete.

        ``None``, not ``b''`` — an empty body is a real body.  Asking again past the end keeps answering ``None``.  A peer that
        vanishes mid-body raises [`ClientDisconnected`][] — a truncated
        upload must never read as a complete one — and so does a body-read
        timeout, which is recorded as a cap hit first.

        The Internals page states the receive-path invariant this implements.
        """
        if self._done:
            return None
        try:
            if self._chunked:
                return await self._next_chunked()
            else:
                # Transport-paced up-to-n slices (env-vars.md,
                # ``BB_BODY_CHUNK_MAX``).  ``b''`` means the peer is gone — a
                # reader parks rather than returning short when more may come —
                # so an unspent Content-Length is a truncated upload.
                if self._content_length:
                    n = min(self._content_length, self._chunk_max)
                    body = await self._read_with_timeout(self._reader.read(n))
                    # Gate on the *length*, not truthiness: forward progress is
                    # the reader's return value, so "zero bytes came back" is
                    # what ends the loop.  Identical for ``bytes``; the
                    # difference is a reader whose reads do not return ``bytes``.
                    if not len(body):
                        raise IncompleteReadError(b'')
                    self._content_length -= len(body)
                    if self._content_length == 0:
                        self._done = True
                    return self._account(body)
                self._done = True
                return None

        except (asyncio.TimeoutError, TimeoutError):
            # Distinguished from EOF mid-body only so operators see the cap hit;
            # the app sees the same HTTP_DISCONNECT either way.
            log_cap_hit('body_timeout',
                        requested=self._body_timeout,
                        limit=self._body_timeout,
                        scope_path=self._req_path,
                        protocol='http1')
            self._done = True
            raise ClientDisconnected() from None
        except EOFError:
            # EOF mid-body — not a cap hit, and no synthetic 408.
            self._done = True
            raise ClientDisconnected() from None

    async def __call__(self) -> dict:
        """The ASGI receive channel: the same body, encoded as event dicts.

        The only place the ``http.request`` dict is built, for whoever wants
        that encoding — a full-form handler calling ``receive()``, or an
        external host.  ``Connection.body()`` / ``stream()`` take
        [`next_chunk`][] and skip the per-chunk dict.

        A Content-Length body ends on its last data event; a chunked body ends
        on a separate empty one.
        """
        if self._done:
            return {'type': ASGIEvent.HTTP_DISCONNECT}
        try:
            chunk = await self.next_chunk()
        except ClientDisconnected:
            return {'type': ASGIEvent.HTTP_DISCONNECT}
        if chunk is None:
            return {'type': ASGIEvent.HTTP_REQUEST, 'body': b'',
                    'more_body': False}
        return {'type': ASGIEvent.HTTP_REQUEST, 'body': chunk,
                'more_body': not self._done}


class HTTP2Recipient(BaseRecipient):
    """Deliver HTTP/2 body events from non-blocking put_DATAFrame calls.

    With credit_callback, replay flow credit only on consumption; never also
    credit at enqueue. Bound queued bytes by credit_budget and retain a frame-count
    abuse cap. A False put refuses the stream. Without the callback, enforce the
    frame-count queue bound and credit at enqueue. END_STREAM on HEADERS requires
    mark_end_of_stream_on_headers so the first read delivers an empty final body.
    """

    def __init__(self, frame: FrameBase | None = None,
                 queue_depth: int = _HTTP2_STREAM_QUEUE_DEPTH,
                 credit_callback: Optional[
                     Callable[[int], Awaitable[None]]] = None,
                 credit_budget: int = DEFAULT_INITIAL_WINDOW_SIZE,
                 max_body: int | None = None,
                 min_rate: float | None = None,
                 min_rate_grace: float | None = None):
        super().__init__(None)
        self._queue: asyncio.Queue | None = None
        self._queue_depth = queue_depth
        self._credit_cb = credit_callback
        self._credit_budget = credit_budget
        # Bytes enqueued but not yet consumed, and therefore not yet credited
        # back.  For a conformant peer this can never exceed ``credit_budget``:
        # the un-credited bytes ARE the closed part of the window it must respect.
        self._uncredited: int = 0
        self._end_of_stream_on_headers: bool = False
        self._initial_consumed: bool = False
        #: A disconnect for a stream whose queue was never needed; delivered
        #: by ``__call__`` after the empty body, as the queue would have.
        self._disconnect_pending: bool = False
        self._done: bool = False
        if max_body is None or min_rate is None or min_rate_grace is None:
            # Fallback for a directly-instantiated recipient (tests).
            _s = get_settings()
            if max_body is None:
                max_body = _s.max_body_size
            if min_rate is None:
                min_rate = _s.min_body_rate
            if min_rate_grace is None:
                min_rate_grace = _s.min_body_rate_grace
        # The two body limits, shared with HTTP/1.1; what each refuses and how
        # HTTP/2 answers it is env-vars.md, ``BB_MAX_BODY_SIZE``.
        self._max_body = max_body
        self._min_rate = min_rate
        self._min_rate_grace = min_rate_grace
        self._body_seen = 0
        #: Fed wall time between DATA frames, which is the peer's alone.
        self._rate_floor: ByteRateFloor | None = None     # made on the first DATA
        self._rate_last: float | None = None
        #: Exempts the peer from the rate judgement: it was blocked by our own
        #: closed inbound window, so its pace is partly our doing.
        self._was_window_stalled = False
        if isinstance(frame, Data):
            self.put_DATAFrame(frame)

    def _ensure_queue(self) -> asyncio.Queue:
        if self._queue is None:
            # Consume-crediting mode bounds itself in put_DATAFrame, so the queue
            # is unbounded and ``put_disconnect`` can always deliver.  Without a
            # credit callback the queue keeps its frame-count maxsize.
            maxsize = 0 if self._credit_cb is not None else self._queue_depth
            self._queue = asyncio.Queue(maxsize=maxsize)
        return self._queue

    def mark_end_of_stream_on_headers(self) -> None:
        """Mark this stream as ended on HEADERS (no body to deliver)."""
        self._end_of_stream_on_headers = True

    @staticmethod
    def make_item(frame: Data) -> tuple[bytes, bool]:
        return frame.payload, bool(frame.end_stream)

    def _body_limits_refuse(self, nbytes: int) -> bool:
        """True when this arrival breaks a body limit and must be refused.

        Judged on arrival rather than on consumption: DATA lands whether or not
        the handler is reading, so the memory this bounds is spent before anyone
        asks for it.

        The rate judgement is skipped once our own inbound window has
        back-pressured the peer — blaming it for obeying our flow control would
        turn a slow *handler* into a reset stream.  A trickle never fills the
        window, which is why the exemption cannot shelter one.
        """
        self._body_seen += nbytes
        if self._max_body and self._body_seen > self._max_body:
            logger.warning('HTTP2Recipient body over BB_MAX_BODY_SIZE — '
                           'refusing DATA frame')
            log_cap_hit('max_body_size',
                        requested=self._body_seen, limit=self._max_body,
                        protocol='http2')
            return True
        if not self._min_rate:
            return False
        now = _monotonic()
        last, self._rate_last = self._rate_last, now
        floor = self._rate_floor
        if floor is None:
            floor = self._rate_floor = ByteRateFloor(self._min_rate, self._min_rate_grace)
        if last is None:
            floor.record(nbytes, 0.0)
            return False
        if (self._credit_cb is not None
                and self._uncredited + nbytes >= self._credit_budget):
            # Noted as the window closes: by the peer's next frame it has reopened.
            self._was_window_stalled = True
        if not floor.record(nbytes, now - last):
            return False
        if self._was_window_stalled:
            floor.reset()       # exempt, but the window still rolls forward
            return False
        logger.warning('HTTP2Recipient body below BB_MIN_BODY_RATE — '
                       'refusing DATA frame')
        log_cap_hit('min_body_rate', requested=floor.observed,
                    limit=self._min_rate, protocol='http2')
        return True

    def put_DATAFrame(self, frame: Data) -> bool:
        """Enqueue a DATA frame event.  Returns False when the frame must be
        refused (the caller answers RST_STREAM): queue full when no credit
        callback is installed; inbound-window overrun, a tiny-frame flood, or
        a body limit (``BB_MAX_BODY_SIZE`` / ``BB_MIN_BODY_RATE``) in
        consume-crediting mode.
        """
        if self._body_limits_refuse(len(frame.payload)):
            return False
        if self._credit_cb is not None:
            # Flow-control debit is the full frame length including padding
            # (RFC 9113 §6.9.1) — credit must mirror it exactly.
            fc_len = frame.length
            if self._uncredited + fc_len > self._credit_budget:
                # Past the advertised inbound window it was never credited for:
                # a conformant peer is back-pressured well before this.
                logger.warning(
                    'HTTP2Recipient inbound window overrun — refusing DATA frame')
                log_cap_hit('h2_inbound_window_budget',
                            requested=self._uncredited + fc_len,
                            limit=self._credit_budget,
                            protocol='http2')
                return False
            queue = self._ensure_queue()
            event_cap = self._queue_depth * _EVENT_CAP_MULTIPLIER
            if queue.qsize() >= event_cap:
                # Zero/tiny-frame flood; see ``_EVENT_CAP_MULTIPLIER``.
                logger.warning(
                    'HTTP2Recipient event-count cap hit — dropping DATA frame')
                log_cap_hit('stream_queue_depth',
                            requested=queue.qsize() + 1,
                            limit=event_cap,
                            protocol='http2')
                return False
            queue.put_nowait((self.make_item(frame), fc_len))
            self._uncredited += fc_len
            return True
        try:
            self._ensure_queue().put_nowait((self.make_item(frame), 0))
            return True
        except asyncio.QueueFull:
            logger.warning('HTTP2Recipient queue full on stream — dropping DATA frame')
            log_cap_hit('stream_queue_depth',
                        requested=self._queue_depth + 1,
                        limit=self._queue_depth,
                        protocol='http2')
            return False

    def put_end_of_stream(self) -> bool:
        """Enqueue an empty final body event when request trailers end the stream.
        """
        return self._put_item((b'', True))

    def _put_item(self, item) -> bool:
        """Enqueue a native queue item.  False when the queue is full."""
        try:
            self._ensure_queue().put_nowait((item, 0))
            return True
        except asyncio.QueueFull:
            logger.warning('HTTP2Recipient queue full on stream — dropping %r', item)
            log_cap_hit('stream_queue_depth',
                        requested=self._queue_depth + 1,
                        limit=self._queue_depth,
                        protocol='http2')
            return False

    def put_disconnect(self) -> None:
        """Unblock a waiting __call__() with an http.disconnect event.

        Without a queue, a stream that ended on HEADERS has no consumer
        waiting, so none is created for the disconnect.
        """
        if self._queue is None and self._end_of_stream_on_headers:
            if not self._initial_consumed:
                self._disconnect_pending = True
            return
        try:
            self._ensure_queue().put_nowait((_H2_DISCONNECT, 0))
        except asyncio.QueueFull:
            # The app task is hopelessly behind; TaskGroup cancellation cleans up
            # the stream regardless.  Unreachable in consume-crediting mode.
            logger.warning('HTTP2Recipient: could not deliver http.disconnect — queue full')

    def take_uncredited(self) -> int:
        """Return and clear the un-consumed credit balance.

        Bytes enqueued but never popped by the app (a handler that finished —
        or was RST — without draining its body).  The actor replays this to
        the CONNECTION window when the stream is released, otherwise the
        shared window leaks shut for every later stream; the stream-level
        window is moot once the stream closes (RFC 9113 §5.1).
        """
        n = self._uncredited
        self._uncredited = 0
        return n

    async def __call__(self) -> dict:
        # Checked even when a queue exists: ``put_disconnect`` may have raced
        # ahead of the app's first read while the stream still ended cleanly at
        # HEADERS, and the complete (empty) body must precede any disconnect —
        # otherwise a body-reading handler on a body-less request sees a
        # spurious client disconnect.
        if self._end_of_stream_on_headers and not self._initial_consumed:
            self._initial_consumed = True
            self._done = True
            return {'type': ASGIEvent.HTTP_REQUEST, 'body': b'', 'more_body': False}
        item = _H2_DISCONNECT if self._disconnect_pending else await self._take()
        if item is _H2_DISCONNECT:
            self._done = True
            return {'type': ASGIEvent.HTTP_DISCONNECT}
        payload, end_stream = item
        # Set here, never read here — internals.md §Receive-path invariant.
        if end_stream:
            self._done = True
        return {'type': ASGIEvent.HTTP_REQUEST, 'body': payload,
                'more_body': not end_stream}

    async def _take(self):
        """Pop the next queue item, replaying consume-time flow-control credit.

        Shared by both channels so the credit contract cannot drift between
        them: the peer is credited when the app *pops*, whichever channel it
        pops through.
        """
        item, credit = await self._ensure_queue().get()
        if credit and self._credit_cb is not None:
            # Decrement before the (interruptible) send so a racing
            # take_uncredited() can never double-credit; worst case a
            # cancellation mid-send under-credits by one frame.
            self._uncredited -= credit
            if (self._was_window_stalled
                    and self._uncredited < self._credit_budget):
                # The exemption marks an *interval* we back-pressured, so it has
                # to end when the window reopens: a flag that only ever turns on
                # would let one window-filling burst buy an unlimited drip.  The
                # window restarts rather than resumes, so the exempted interval
                # is not averaged into the next judgement.
                self._was_window_stalled = False
                if self._rate_floor is not None:
                    self._rate_floor.reset()
                self._rate_last = None
            try:
                await self._credit_cb(credit)
            except Exception:
                # Connection closing/gone — credit no longer matters; the
                # disconnect event is the authoritative teardown signal.
                logger.debug('consume-time WINDOW_UPDATE replay failed',
                             exc_info=True)
        return item

    async def next_chunk(self) -> bytes | None:
        """The next body chunk, or ``None`` once the stream has ended.

        The H2 half of the native receive channel — same contract as
        [`HTTP1Recipient.next_chunk`][HTTP1Recipient.next_chunk], so ``Connection.body()`` /
        ``stream()`` read one protocol and get both.
        """
        if self._end_of_stream_on_headers and not self._initial_consumed:
            self._initial_consumed = True
            self._done = True
            return None
        if self._done:
            return None
        item = await self._take()
        if item is _H2_DISCONNECT:
            self._done = True
            raise ClientDisconnected()
        payload, end_stream = item
        if end_stream:
            self._done = True
        return payload


class WebSocketRecipient(BaseRecipient):
    """Receive complete WebSocket messages, after an initial connect event.

    ws_queue_depth=0 reads inline when the app calls receive(); a positive depth
    uses a bounded read-ahead queue. Both deliver the same events, but inline
    PING/CLOSE servicing waits for a read or the control watchdog. Registering
    a websocket_message observer uses the deferred-reader path described in
    docs/guide/websockets.md; it does not enable eager read-ahead.
    """

    _MAX_FRAME_PAYLOAD: int = 64 * 1024 * 1024

    _MAX_MESSAGE_SIZE: int = 16 * 1024 * 1024

    def __init__(self, reader: AbstractReader, writer: AbstractWriter, *,
                 require_masked: bool = True,
                 dispatcher: EventDispatcher | None = None,
                 conn: Connection | None = None,
                 ws_queue_depth: int = _WS_READ_INLINE,
                 decompressor=None,
                 max_frame_payload: int | None = None,
                 max_message_size: int | None = None,
                 on_message: Callable[[dict], Awaitable[None]] | None = None,
                 read_ahead_needed: Callable[[], bool] | None = None,
                 ws_idle_timeout: float | None = None,
                 ws_pong_timeout: float | None = None,
                 frame_cap_name: str = 'ws_max_frame_payload',
                 message_cap_name: str = 'ws_max_message_size'):
        super().__init__(reader)
        self._writer = writer
        self._frame_cap_name = frame_cap_name
        self._message_cap_name = message_cap_name
        self._connect_sent = False
        # Late import keeps ``recipient`` importable without the settings stack,
        # for callers that drive a recipient with no Settings populated.
        if max_frame_payload is not None:
            self._max_frame_payload: int = max_frame_payload
        else:
            try:
                self._max_frame_payload = get_settings().ws_max_frame_payload
            except Exception:
                self._max_frame_payload = self._MAX_FRAME_PAYLOAD
        if max_message_size is not None:
            self._max_message_size: int = max_message_size
        else:
            try:
                self._max_message_size = get_settings().ws_max_message_size
            except Exception:
                self._max_message_size = self._MAX_MESSAGE_SIZE
        self._assembler = FragmentAssembler(max_total=self._max_message_size)
        # Per connection, not shared: the budget is what *one* peer may spend.
        try:
            _s = get_settings()
            self._control_meter = RateWindow(_s.frame_rate_limit,
                                             _s.frame_rate_window)
        except Exception:
            self._control_meter = RateWindow(20, 1.0)
        # RFC 6455 §5.1 — a client MUST mask, a server MUST NOT.  Masking is
        # therefore symmetric in one flag: whoever requires it *in* must not mask
        # *out*, so this also decides the recipient's own PONG and CLOSE frames.
        self._require_masked = require_masked
        self._dispatcher = dispatcher
        self._conn = conn
        self._ws_queue_depth = ws_queue_depth
        self._event_queue: asyncio.Queue | None = None
        self._reader_task: asyncio.Task | None = None
        # Inline mode's handoff slot, not a queue: it holds at most one item,
        # because the inline driver stops as soon as a frame produces something.
        # The bounded read-ahead the depth knob describes is eager mode only.
        self._pending: deque = deque()
        # Stops the inline driver from touching a dead transport.
        self._read_finished = False
        # Post-terminal behaviour, identical in both modes: once the terminal
        # event is handed to the app, receive() keeps answering a disconnect with
        # this code instead of blocking forever.
        self._terminal_code: int | None = None
        self._terminal_delivered = False
        # ``None`` disables permessage-deflate, which makes any inbound RSV1=1
        # frame a protocol violation.
        self._decompressor = decompressor
        # Read-time emit adapter for ``websocket_message`` (server path); the
        # dispatcher/conn pair below is the equivalent for a direct drive.
        self._on_message = on_message
        self._read_ahead_needed = read_ahead_needed
        # Wire ownership: exactly one of {inline receive, reader task, watchdog
        # servicing} drives the wire at a time.  The reader task and the watchdog
        # both yield on these two flags.
        self._reading = False
        self._servicing = False
        # A control frame has been read or peeked on this connection.  Gates the
        # per-message watchdog work, which before the first one is pure overhead
        # — an echo workload pays one bool check per message instead.
        self._saw_control_frame = False
        self._listeners = False
        # A listener needs read-ahead but the reader task has not started; the
        # idle watchdog starts it if the app goes quiet.
        self._deferred_pending = False
        # Created lazily on the first touch(), so constructing a recipient never
        # requires a running loop.
        self._watchdog: Deadline | None = None
        self._closed = False
        # The *time* column for a WebSocket connection (env-vars.md,
        # ``BB_WS_IDLE_TIMEOUT``).  Off unless a caller asks: this class is the
        # read side of both roles, and ``RecipientFactory.websocket`` — the
        # server's entry point — is the one place the Settings value is read.
        self._ws_idle_timeout: float = ws_idle_timeout or 0.0
        self._ws_pong_timeout: float = ws_pong_timeout or 30.0
        self._inbound_seq: int = 0
        self._seq_at_last_check: int = 0
        self._last_inbound_at: float = 0.0
        self._probe_sent_at: float | None = None

    @property
    def terminal_code(self) -> int | None:
        """Return the close code once the read side finishes.
        """
        return self._terminal_code

    async def _emit(self, item) -> None:
        """Hand one ASGI event (or an exception to re-raise app-side) to the app.

        The only place the two read modes diverge: eager mode pushes through the
        bounded queue, which is what applies backpressure to a fast peer; inline
        mode drops it in the handoff slot, where the caller one frame up the
        stack is already waiting for it.

        The terminal code is recorded here so a receive() past the terminal event
        keeps answering a disconnect with the same code.
        """
        if isinstance(item, ProtocolError):
            self._terminal_code = item.close_code
        elif isinstance(item, Exception):
            self._terminal_code = WSCloseCode.ABNORMAL
        if self._event_queue is not None:
            await self._event_queue.put(item)
        else:
            self._pending.append(item)

    async def _read_step(self) -> bool:
        """Process one frame; return True only when the read side is finished.
        """
        _CONTROL_OPS = (WSOpcode.CLOSE, WSOpcode.PING, WSOpcode.PONG)
        h = await read_frame_header(self._reader)
        self._inbound_seq += 1
        self._probe_sent_at = None

        # RFC 6455 §5.5 — control frames MUST have payload ≤125 and
        # MUST NOT be fragmented.  Reject without reading the body.
        if h.opcode in _CONTROL_OPS:
            if not h.fin:
                raise ProtocolError('fragmented control frame')
            if h.length > 125:
                raise ProtocolError(
                    f'control frame payload {h.length} > 125')
            # Metered by count (env-vars.md, ``BB_FRAME_RATE_LIMIT``), and
            # checked before the payload is read: the answer to too many is to
            # stop, not to keep reading them faster.
            if self._control_meter.hit():
                log_cap_hit('frame_rate',
                            requested=self._control_meter.count,
                            limit=self._control_meter.limit,
                            scope_path=self._conn.path if self._conn else None,
                            protocol='ws')
                raise ProtocolError(
                    f'control frame rate limit exceeded '
                    f'({self._control_meter.count} in '
                    f'{self._control_meter.window}s)',
                    close_code=WSCloseCode.POLICY_VIOLATION)

        # RFC 6455 §5.2 — RSV bits MUST be 0 unless an extension defining them
        # was negotiated.  RSV1 is permessage-deflate's (RFC 7692); RSV2/RSV3
        # belong to no extension we negotiate, and RSV1 on a control frame is
        # always a violation per RFC 7692 §6.
        if h.rsv2 or h.rsv3:
            raise ProtocolError(
                f'RSV2/RSV3 set without negotiated extension '
                f'(rsv2={h.rsv2} rsv3={h.rsv3})')
        if h.rsv1 and (self._decompressor is None or h.opcode in _CONTROL_OPS):
            raise ProtocolError(
                f'RSV1 set on frame (opcode={h.opcode}) without '
                f'negotiated permessage-deflate')

        # ``h.length`` is the wire indicator (0–125, 126 or 127); the resolved
        # extended length is judged inside ``read_payload``, which raises before
        # any body byte is read off the wire.
        try:
            payload = await read_payload(
                self._reader, h.masked, h.length,
                max_length=self._max_frame_payload)
        except FramePayloadTooLarge as exc:
            log_cap_hit(self._frame_cap_name,
                        requested=exc.declared,
                        limit=self._max_frame_payload,
                        scope_path=self._conn.path if self._conn else None,
                        protocol='ws')
            raise ProtocolError(
                str(exc),
                close_code=WSCloseCode.MESSAGE_TOO_BIG,
            ) from exc
        except InvalidFrameLength as exc:
            raise ProtocolError(str(exc)) from exc

        if self._require_masked and not h.masked:
            raise ProtocolError('unmasked client frame')

        match h.opcode:
            case WSOpcode.TEXT | WSOpcode.BINARY | WSOpcode.CONTINUATION:
                # A data frame is never terminal.
                await self._handle_data_frame(h.opcode, payload, h.fin, h.rsv1)
                return False
            case WSOpcode.CLOSE | WSOpcode.PING | WSOpcode.PONG:
                return await self._handle_control_frame(h.opcode, payload)
            case _:
                await self._handle_unknown_opcode()
                return True
        # Unreachable — the wildcard arm covers every opcode.  Present so every
        # path has an explicit ``bool`` return (the CodeQL mixed-returns rule).
        return False

    async def _drive_once(self) -> bool:
        """One [`_read_step`][] under the shared error handling.

        Every failure path is terminal and emits exactly one thing for the app
        — a disconnect event or the exception itself — so both drivers can
        treat a True return as "stop reading" without duplicating any of the
        RFC 6455 close-frame handling.
        """
        try:
            return await self._read_step()
        except (asyncio.IncompleteReadError, IncompleteReadError):
            await self._close_channel(WSCloseCode.ABNORMAL)
            return True
        except ProtocolError as exc:
            await self._send_close(exc.close_code)
            await self._emit_disconnected(exc.close_code)
            # Any exception in the read loop is raised back to the app on its
            # next receive(); the close frame has already gone out.
            await self._emit(exc)
            return True
        except Exception as exc:
            await self._send_close(WSCloseCode.INTERNAL_ERROR)
            await self._emit(exc)
            return True

    async def _read_loop(self) -> None:
        """Eager driver: read ahead of the app until the read side is done."""
        while not self._read_finished:
            self._read_finished = await self._drive_once()

    def _refuse_oversized_message(self, exc: MessageTooLarge) -> NoReturn:
        """Log the cap hit and raise the 1009 that closes the connection.

        Three ways to outgrow the bound — fragment total, inflate output, a
        single oversized frame — and one thing to do about it.
        """
        log_cap_hit(self._message_cap_name,
                    requested=exc.produced,
                    limit=exc.maximum,
                    scope_path=self._conn.path if self._conn else None,
                    protocol='ws')
        raise ProtocolError(str(exc), close_code=WSCloseCode.MESSAGE_TOO_BIG) from exc

    async def _handle_data_frame(self, opcode, payload: bytes, fin: bool,
                                 rsv1: bool = False) -> None:
        """Handle TEXT/BINARY/CONTINUATION frame.

        Returns nothing deliberately: whether a message was emitted is not a
        signal either driver needs, and a ``bool`` here would invite a reader to
        mistake "a message was emitted" for "the read side is done".
        """
        try:
            result = self._assembler.feed(opcode, payload, fin, rsv1)
        except MessageTooLarge as exc:
            self._refuse_oversized_message(exc)
        if result is None:
            return
        msg_opcode, full_payload, compressed = result
        if compressed:
            assert self._decompressor is not None  # frame loop enforced this
            try:
                full_payload = self._decompressor.decompress(
                    full_payload, max_length=self._max_message_size or None)
            except MessageTooLarge as exc:
                # Ordered before the generic handler on purpose: an inflate
                # bomb is a size refusal (1009), not corrupt data (1002).
                self._refuse_oversized_message(exc)
            except Exception as exc:
                # RFC 7692 §7.1 — a payload that fails to decompress is a
                # connection error.  Treat as PROTOCOL_ERROR (1002).
                raise ProtocolError(
                    f'permessage-deflate decompression failed: {exc}',
                    close_code=1002,
                ) from exc
        elif self._max_message_size and len(full_payload) > self._max_message_size:
            # An unfragmented, uncompressed frame reaches neither bound above:
            # the assembler passes it straight through and there is no inflate
            # step.  Without this the message total would be weaker than the
            # frame cap for the simplest message there is.
            self._refuse_oversized_message(
                MessageTooLarge(len(full_payload), self._max_message_size))
        if msg_opcode == WSOpcode.TEXT:
            try:
                text = full_payload.decode('utf-8')
            except UnicodeDecodeError as e:
                # RFC 6455 §8.1 — invalid UTF-8 in a TEXT message MUST be
                # treated as a CLOSE with status code 1007.
                raise ProtocolError(f'invalid UTF-8 in TEXT message: {e}',
                                    close_code=1007)
            message: str | bytes = text
        else:
            message = full_payload
        # ``websocket_message`` fires HERE, when the message is read, in every
        # mode.  The guard is re-evaluated per message rather than read from the
        # receive-cycle cache: a listener registered while the app's
        # ``receive()`` was blocked on the wire is only visible to a fresh check.
        if (self._on_message is not None
                and (self._read_ahead_needed is None
                     or self._read_ahead_needed())):
            await self._on_message(message)
        elif self._dispatcher is not None and self._conn is not None:
            is_text = isinstance(message, str)
            await self._dispatcher.emit(Event(
                'websocket_message',
                detail={
                    'conn': self._conn,
                    'text': message if is_text else None,
                    'bytes': None if is_text else message,
                },
            ))
        await self._emit(message)

    async def _handle_control_frame(self, opcode, payload: bytes) -> bool:
        """Handle CLOSE/PING/PONG frame; returns True if the connection should close."""
        if opcode == WSOpcode.CLOSE:
            # RFC 6455 §5.5.1 — an endpoint that has not sent one MUST answer a
            # Close frame, echoing the peer's status code if present; on any
            # violation of the code or the reason text, send 1002 instead.
            code, reason_ok = _parse_close_payload(payload)
            if not reason_ok:
                code = WSCloseCode.PROTOCOL_ERROR
            await self._send_close(code)
            await self._close_channel(code)
            return True
        if opcode == WSOpcode.PING:
            pong = self._encode_frame(payload, WSOpcode.PONG)
            await self._writer.write(pong)
        # PONG: unsolicited pong — silently drop
        return False

    async def _handle_unknown_opcode(self) -> None:
        """Send a CLOSE frame and emit a disconnect event for an unknown opcode."""
        await self._send_close(WSCloseCode.PROTOCOL_ERROR)
        await self._close_channel(WSCloseCode.PROTOCOL_ERROR)

    def _encode_frame(self, payload: bytes, opcode: WSOpcode | int) -> bytes:
        """A frame this end sends: masked unless this end is the server."""
        return encode_frame(payload, opcode=opcode,
                            mask=not self._require_masked)

    async def _send_close(self, code: int) -> None:
        """Best-effort CLOSE frame: the peer's socket may already be gone."""
        close = self._encode_frame(code.to_bytes(2, 'big'), WSOpcode.CLOSE)
        with contextlib.suppress(Exception):
            await self._writer.write(close)

    async def _close_channel(self, code: int) -> None:
        """Fire ``websocket_disconnected`` and end the channel with *code*.

        The close code is recorded once here and both channels read it from
        there; passing it twice would be one place for the two to disagree.
        """
        await self._emit_disconnected(code)
        self._terminal_code = code
        await self._emit(_WS_CLOSED)

    async def _emit_disconnected(self, code: int) -> None:
        """Emit websocket_disconnected exactly once per connection.

        The de-dup flag rides the same client-disconnect marker the HTTP path
        uses (``disconnected``/``mark_disconnected``), so a native Connection
        needs no WS-specific extra.
        """
        conn = self._conn
        if (self._dispatcher is not None and conn is not None
                and not disconnected(conn)):
            mark_disconnected(conn)
            client = conn.client
            connection_id, path = conn.connection_id, conn.path
            await self._dispatcher.emit(Event(
                'websocket_disconnected',
                detail={
                    'conn':          conn,
                    'connection_id': connection_id,
                    'client_ip':     client[0] if client else '',
                    'path':          path,
                    'code':          code,
                },
            ))

    def _read_ahead_observed(self) -> bool:
        if self._read_ahead_needed is not None:
            return self._listeners
        return (self._dispatcher is not None
                and self._dispatcher.has_listeners('websocket_message'))

    def _start_reader(self, depth: int) -> None:
        """Create the read-ahead queue and its task, carrying over the handoff.

        Once the queue exists the app reads from it alone, so anything left in
        ``_pending`` must move across or it is never delivered.  It holds at most
        one item, so the bounded queue cannot overflow here.
        """
        queue: asyncio.Queue = asyncio.Queue(maxsize=depth)
        while self._pending:
            queue.put_nowait(self._pending.popleft())
        self._event_queue = queue
        self._reader_task = asyncio.create_task(self._read_loop())
        self._deferred_pending = False

    def _ensure_reader_started(self) -> None:
        """Start the read-ahead task, or mark it deferred.

        A positive ``ws_queue_depth`` is an explicit opt-in and starts the reader
        now.  A listener that merely *needs* read-ahead does not: the contract it
        depends on is that the message is read, not that it is read ahead, and a
        consuming handler is already reading (env-vars.md, ``BB_WS_QUEUE_DEPTH``).
        """
        if self._event_queue is not None or self._read_finished:
            return
        if self._ws_queue_depth > 0:
            self._start_reader(self._ws_queue_depth)
        elif self._read_ahead_observed():
            self._deferred_pending = True

    def start_deferred_reader(self) -> None:
        """Start the deferred reader task.

        Idempotent and safe: refuses while a reader already owns the wire,
        while the app is mid-read, or after the read side terminated.

        A listener can need read-ahead with the depth left at 0, so the queue
        falls back to the standard depth — a 0-maxsize queue is unbounded,
        which would drop the backpressure bound.
        """
        if (not self._deferred_pending or self._event_queue is not None
                or self._reader_task is not None or self._reading
                or self._read_finished):
            return
        self._start_reader(self._ws_queue_depth or _WS_EVENT_QUEUE_DEPTH)

    def _frame_bytes_needed(self) -> int | None:
        """Bytes required for the next *complete* frame, or None when it is not
        fully buffered.  The non-blocking guarantee of
        [`service_available_control_frames`][] rests on this."""
        buffered = self._reader.buffered_len()
        if buffered < 2:
            return None
        head = self._reader.peek(2)
        len_code = head[1] & 0x7F
        ext = 2 if len_code == 126 else (8 if len_code == 127 else 0)
        if buffered < 2 + ext + 4:                # header + mask key
            return None
        if ext:
            payload_len = int.from_bytes(self._reader.peek(2 + ext)[2:], 'big')
        else:
            payload_len = len_code
        total = 2 + ext + 4 + payload_len
        return total if buffered >= total else None

    async def service_available_control_frames(self) -> bool:
        """Non-blocking servicing of fully-buffered inbound control frames.

        Answers PINGs and echoes CLOSE that arrived while the handler was
        doing something other than ``receive()`` (send-time servicing) or
        after it went quiet (the idle watchdog).  Reads only frames already
        fully buffered, so it never blocks and never steals the wire from an
        inline ``receive()`` (guarded by ``_reading`` / ``_servicing``).  A
        data frame stops the loop without being consumed — the app or a
        reader owns it.  Returns True if any frame was serviced.
        """
        if (self._servicing or self._reading or self._read_finished
                or self._event_queue is not None):
            # A reader task owns the wire when ``_event_queue`` is set — it
            # services control frames itself; the watchdog must not race it.
            return False
        if not self._reader.has_buffered():
            return False
        self._servicing = True
        try:
            serviced = False
            while (not self._read_finished and not self._reading
                   and self._reader.has_buffered()):
                if self._frame_bytes_needed() is None:
                    break
                head = self._reader.peek(2)
                if (head[0] & 0x0F) not in _WS_CONTROL_OPS:
                    break                     # data frame — owned by app/reader
                self._read_finished = await self._drive_once()
                serviced = True
            return serviced
        finally:
            self._servicing = False

    def _on_idle_tick(self) -> None:
        """Watchdog callback: the connection has been quiet for a tick.

        Runs from the scanner's timer context — cheap checks only, then a task
        for any actual work.  Only a pure inline connection needs the servicing
        help; a reader that owns the wire does it itself.

        Liveness runs **first and unconditionally**, because it asks a different
        question: servicing is about a connection whose handler has gone quiet,
        liveness about one whose *peer* has.  A reader parked on the wire is the
        normal shape of a silent peer, so the guards below would exempt exactly
        the case the probe exists for.
        """
        self._check_liveness()
        if self._read_finished or self._reading or self._closed:
            return
        if self._event_queue is not None:
            return
        if self._deferred_pending:
            self.start_deferred_reader()
        elif self._reader.has_buffered() and self.has_control_frames_buffered():
            asyncio.get_running_loop().create_task(
                self.service_available_control_frames())

    def _check_liveness(self) -> None:
        """Ask a silent peer whether it is there; close it if it is not.

        One clock read per idle connection per scanner tick, and none on the
        receive path: ``_inbound_seq`` moving is what "activity" means, and
        this is the only place it is turned into a time.
        """
        if self._ws_idle_timeout <= 0 or self._closed or self._read_finished:
            return
        loop = asyncio.get_running_loop()
        now = loop.time()
        if self._inbound_seq != self._seq_at_last_check:
            # Frames arrived since the last tick; the read path has already
            # cleared any outstanding probe.
            self._seq_at_last_check = self._inbound_seq
            self._last_inbound_at = now
            return
        if self._last_inbound_at == 0.0:
            # First quiet tick: the clock starts here rather than at
            # construction, so a connection that was busy before the watchdog
            # armed is not judged on time it never spent idle.
            self._last_inbound_at = now
            return
        if self._probe_sent_at is not None:
            if now - self._probe_sent_at >= self._ws_pong_timeout:
                loop.create_task(self._end_for_unresponsive_peer())
            return
        if now - self._last_inbound_at >= self._ws_idle_timeout:
            self._probe_sent_at = now
            loop.create_task(self._probe_peer())

    async def _probe_peer(self) -> None:
        """RFC 6455 §5.5.2 — ask whether the peer is still responsive.

        §5.5.3 obliges a PONG in reply, but any inbound frame clears the
        probe: a peer that is talking to us is alive, and requiring the
        specific answer would close a connection that is merely busy.
        """
        ping = self._encode_frame(b'', WSOpcode.PING)
        with contextlib.suppress(Exception):
            await self._writer.write(ping)

    async def _end_for_unresponsive_peer(self) -> None:
        """Close an unanswered liveness probe with Going Away (1001).
        """
        if self._closed or self._read_finished:
            return
        logger.info('WebSocket peer did not answer the liveness PING in '
                    '%.1fs — closing 1001', self._ws_pong_timeout)
        await self._send_close(WSCloseCode.GOING_AWAY)
        self.disarm_watchdog()
        await self._close_channel(WSCloseCode.GOING_AWAY)

    def has_buffered(self) -> bool:
        """True when inbound bytes are already buffered (a read won't block).

        The actor's send-time servicing probes this so it skips the servicing
        call entirely on the common empty-buffer path.
        """
        return self._reader.has_buffered()

    def has_control_frames_buffered(self) -> bool:
        """True when a control frame leads the inbound buffer.

        Synchronous, O(1) gate for send-time servicing: with only data
        frames buffered (a flood), the servicing coroutine's flag churn and
        ``_frame_bytes_needed`` scan would run per message for nothing — a
        data frame is owned by the app/reader, not the servicer.  Also marks
        the connection as having observed a control frame, which activates
        the per-message watchdog work.
        """
        if self._reader.buffered_len() < 2:
            return False
        is_ctrl = (self._reader.peek(2)[0] & 0x0F) in _WS_CONTROL_OPS
        if is_ctrl:
            self._saw_control_frame = True
        return is_ctrl

    def send_touch(self) -> None:
        """Mark send activity for the idle watchdog.

        Does nothing until control frames matter or a listener needs the
        deferred reader; ``touch()`` re-arms a missing watchdog, so a send
        before the connect receive is still safe.  There is deliberately no
        send-time servicing fast path — the watchdog alone bounds PONG latency
        to ~one scanner tick, which is the documented contract.
        """
        if self._deferred_pending or self._saw_control_frame:
            self.touch()

    def _ensure_watchdog(self) -> None:
        if self._watchdog is None:
            self._watchdog = Deadline(self._on_watchdog)

    def _on_watchdog(self) -> None:
        """Quiet for a tick: service deferred work, and keep watching."""
        self._watchdog.arm(_deadline.TICK_S)
        self._on_idle_tick()

    def _ensure_watchdog_armed(self) -> None:
        """Create + register the watchdog once (requires a running loop).

        Arming must not depend on a touch: the zero-listener echo never touches,
        yet an idle connection with a buffered control frame must still be
        serviced.
        """
        if self._watchdog is None:
            self._ensure_watchdog()
            self._watchdog.arm(_deadline.TICK_S)

    def touch(self) -> None:
        """Mark connection activity (receive or send) for the idle watchdog."""
        self._ensure_watchdog()
        self._watchdog.arm(_deadline.TICK_S)

    def disarm_watchdog(self) -> None:
        if self._watchdog is not None:
            self._watchdog.disarm()

    async def shutdown(self) -> None:
        """Disarm the watchdog and cancel the read loop, waiting up to 5 seconds.

        Safe to call repeatedly or before reading starts; never joins itself.
        The caller closes the transport even if the reader does not stop.
        """
        self._closed = True
        self.disarm_watchdog()
        task = self._reader_task
        self._reader_task = None
        if (task is not None and task is not asyncio.current_task()
                and not task.done()):
            task.cancel()
            # wait_for would also wait for cancellation to finish.
            done, _pending = await asyncio.wait(
                {task}, timeout=_READ_LOOP_JOIN_TIMEOUT)
            if not done:
                logger.warning(
                    'read-loop task did not stop within %.1fs; leaving it to '
                    'read a dead transport', _READ_LOOP_JOIN_TIMEOUT)
            else:
                try:
                    task.result()
                except asyncio.CancelledError:
                    pass  # Expected: the task was cancelled intentionally.

    def _mark_connect_sent(self) -> None:
        """Claim the handshake read and arm timers once, shared by both channels.
        """
        self._connect_sent = True
        self._ensure_watchdog_armed()
        self._ensure_reader_started()

    def _refresh_listeners(self) -> None:
        ra = self._read_ahead_needed
        self._listeners = (ra is None) or ra()

    async def _next_item(self):
        """Pop the next thing the read side produced, or ``_WS_CLOSED``.

        The shared body of both channels: a complete message (``str`` /
        ``bytes``), an exception to re-raise app-side, or the close marker.
        Everything above this is encoding; everything below it is the wire.
        """
        if self._deferred_pending or self._saw_control_frame:
            self.touch()
        self._ensure_reader_started()
        if self._terminal_delivered:
            return _WS_CLOSED
        if self._event_queue is not None:
            return await self._event_queue.get()
        # Only one reader may own the wire across awaits. Inline mode services control
        # frames at the application read cadence.
        self._reading = True
        try:
            while not self._pending and not self._read_finished:
                self._read_finished = await self._drive_once()
        finally:
            self._reading = False
        if not self._pending:
            # The read side finished without leaving anything: the app is
            # reading past the terminal event it already got.
            self._terminal_delivered = True
            return _WS_CLOSED
        return self._pending.popleft()

    async def await_connect(self) -> None:
        """Consume the opening handshake on the native channel.

        The raw ``(conn, receive, send)`` form reads a ``websocket.connect``
        dict for this; the object form has no use for the envelope, so the
        native channel just records that the handshake was taken.  A peer that
        gave up mid-handshake raises [`WebSocketDisconnect`][], the same
        signal [`next_message`][] gives.
        """
        self._refresh_listeners()
        if not self._connect_sent:
            self._mark_connect_sent()
            return
        # Already consumed — the caller is re-entering; surface the terminal
        # state rather than silently eating the client's first message.
        if self._terminal_delivered or self._read_finished:
            raise _ws_disconnect(self._terminal_code)

    async def next_message(self) -> str | bytes:
        """The next complete application message: ``str`` text, ``bytes`` binary.

        The native receive channel.  Fragments are already reassembled
        (RFC 6455 §5.4), so what comes back is always a whole message, and the
        Python type *is* the text/binary discriminator — the same contract
        [`blackbull.websocket.WebSocket.receive`][blackbull.websocket.WebSocket.receive] publishes.

        Raises [`WebSocketDisconnect`][blackbull.websocket.WebSocketDisconnect] when the peer
        closes, carrying the RFC 6455 §7.4 status code, and re-raises a
        [`ProtocolError`][] the read side recorded.
        """
        self._refresh_listeners()
        if not self._connect_sent:
            self._mark_connect_sent()
        item = await self._next_item()
        if item is _WS_CLOSED:
            self._terminal_delivered = True
            raise _ws_disconnect(self._terminal_code)
        if isinstance(item, Exception):
            self._terminal_delivered = True
            raise item
        return item

    async def __call__(self) -> dict:
        """The ASGI receive channel: the same messages, encoded as dicts.

        The only place a ``websocket.*`` receive dict is built, for whoever
        wants that encoding — a raw ``(conn, receive, send)`` handler, or an
        external host.  The object form is [`next_message`][].
        """
        self._refresh_listeners()
        if not self._connect_sent:
            self._mark_connect_sent()
            return {'type': ASGIEvent.WS_CONNECT}
        item = await self._next_item()
        if item is _WS_CLOSED:
            self._terminal_delivered = True
            return {'type': ASGIEvent.WS_DISCONNECT,
                    'code': self._terminal_code or WSCloseCode.ABNORMAL}
        if isinstance(item, Exception):
            self._terminal_delivered = True
            raise item
        if isinstance(item, str):
            return {'type': ASGIEvent.WS_RECEIVE, 'text': item, 'bytes': None}
        return {'type': ASGIEvent.WS_RECEIVE, 'text': None, 'bytes': item}



# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

class RecipientFactory:
    """Creates the appropriate ``BaseRecipient`` for the given protocol."""

    @staticmethod
    def http1(reader: AbstractReader, conn: Connection, *,
              body_timeout: float = 0.0,
              deadline: Guard | None = None,
              framing: tuple[int | None, bool]) -> HTTP1Recipient:
        return HTTP1Recipient(reader, conn, body_timeout=body_timeout,
                              deadline=deadline, framing=framing)

    @staticmethod
    def http2(frame: FrameBase | None = None,
              queue_depth: int = _HTTP2_STREAM_QUEUE_DEPTH,
              credit_callback: Optional[
                  Callable[[int], Awaitable[None]]] = None,
              credit_budget: int = DEFAULT_INITIAL_WINDOW_SIZE,
              max_body: int | None = None,
              min_rate: float | None = None,
              min_rate_grace: float | None = None) -> HTTP2Recipient:
        # Forwarded rather than left to the recipient's own per-stream fallback;
        # see ``HTTP2Recipient.__init__``.
        return HTTP2Recipient(frame, queue_depth=queue_depth,
                              credit_callback=credit_callback,
                              credit_budget=credit_budget,
                              max_body=max_body, min_rate=min_rate,
                              min_rate_grace=min_rate_grace)

    @staticmethod
    def websocket(reader: AbstractReader, writer, *,
                  dispatcher: EventDispatcher | None = None,
                  conn: Connection | None = None,
                  ws_queue_depth: int = _WS_READ_INLINE,
                  decompressor=None,
                  on_message: Callable[[dict], Awaitable[None]] | None = None,
                  read_ahead_needed: Callable[[], bool] | None = None) -> WebSocketRecipient:
        if not isinstance(writer, AbstractWriter):
            writer = AsyncioWriter(writer)
        # The liveness probe is read here, not in the recipient: this factory is
        # the *server's* entry point, and the probe answers a question only the
        # server has — how long an untrusted peer may hold a connection.
        _cfg = get_settings()
        return WebSocketRecipient(reader, writer, dispatcher=dispatcher, conn=conn,
                                  ws_queue_depth=ws_queue_depth,
                                  decompressor=decompressor,
                                  on_message=on_message,
                                  read_ahead_needed=read_ahead_needed,
                                  ws_idle_timeout=_cfg.ws_idle_timeout,
                                  ws_pong_timeout=_cfg.ws_pong_timeout)
