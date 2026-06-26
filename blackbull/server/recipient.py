import asyncio
from abc import ABC, abstractmethod

from .cap_log import log_cap_hit
from .deadline import ConnectionDeadline
from .sender import AbstractWriter, AsyncioWriter
from .ws_codec import (
    FramePayloadTooLarge, WSOpcode, encode_frame, read_frame_header,
    read_payload,
)
from .constants import WSCloseCode
from ..asgi import ASGIEvent
from ..headers import Headers
from ..protocol.frame_types import FrameBase, Data
from ..event import Event, EventDispatcher
import logging

logger = logging.getLogger(__name__)

# Per-stream and per-connection event queue depth limits.
# These cap memory growth under overload; see Phase 0 in bench/README.md.
_HTTP2_STREAM_QUEUE_DEPTH = 64
_WS_EVENT_QUEUE_DEPTH = 256


# ---------------------------------------------------------------------------
# Reader abstraction — swap asyncio for trio/curio by implementing this ABC
# ---------------------------------------------------------------------------

class IncompleteReadError(EOFError):
    """Raised by AbstractReader when the peer closes the connection mid-read.

    Mirrors asyncio.IncompleteReadError but is not tied to asyncio, so
    handlers that depend on AbstractReader remain runtime-agnostic.
    """


_HEXDIG_SET = frozenset(b'0123456789abcdefABCDEF')


def _parse_chunk_size(line: bytes) -> int:
    """RFC 9112 §7.1.1 — ``chunk-size = 1*HEXDIG``, optionally followed
    by ``chunk-ext`` (everything from the first ``;``).  Reject anything
    that isn't a non-empty hexadecimal string in the size portion.

    Raises :class:`ValueError` on malformed input; the caller turns that
    into a connection-closing failure (the request body is now
    unframeable).
    """
    # Drop trailing CRLF and optional chunk-ext.
    line = line.rstrip(b'\r\n')
    size_part, _, _ext = line.partition(b';')
    size_part = size_part.rstrip(b' \t')  # OWS between size and ';' is allowed
    if not size_part or any(c not in _HEXDIG_SET for c in size_part):
        raise ValueError(f'invalid chunk-size {size_part!r}')
    return int(size_part, 16)


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
        # RFC §5.5.1: when a Close frame contains a status code, the code
        # MUST be 2 octets; a 1-octet payload is malformed.
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

    def at_eof(self) -> bool:
        """Return True once the peer has closed and the buffer is drained.

        Default ``False`` (callers that need EOF detection — e.g. a long-lived
        raw-protocol read loop — should use a reader that overrides this).
        """
        return False

    async def readuntil(self, sep: bytes) -> bytes:
        """Read until *sep* is seen (inclusive).  Default: byte-wise via
        :meth:`read`.  Concrete transport readers override this with the
        stream's native, buffered implementation."""
        buf = bytearray()
        while sep not in buf:
            chunk = await self.read(1)
            if not chunk:
                break
            buf += chunk
        return bytes(buf)

    async def readexactly(self, n: int) -> bytes:
        """Read exactly *n* bytes.  Default: accumulate via :meth:`read`.
        Concrete transport readers override this."""
        buf = bytearray()
        while len(buf) < n:
            chunk = await self.read(n - len(buf))
            if not chunk:
                break
            buf += chunk
        return bytes(buf)


class AsyncioReader(AbstractReader):
    """Adapts an asyncio-compatible stream to ``AbstractReader``.

    Accepts any object exposing ``read()``, ``readuntil()``, and
    ``readexactly()`` — the asyncio StreamReader API — so that test doubles
    such as ``MagicMock`` can be injected without ceremony.
    """

    def __init__(self, stream_reader):
        if not (hasattr(stream_reader, 'read') and hasattr(stream_reader, 'readuntil')):
            raise TypeError(
                f"AsyncioReader requires an object with read() and readuntil(), "
                f"got {type(stream_reader)}"
            )
        self._sr = stream_reader

    async def read(self, n: int) -> bytes:
        try:
            return await self._sr.read(n)
        except asyncio.IncompleteReadError as exc:
            raise IncompleteReadError(exc.partial) from exc

    def at_eof(self) -> bool:
        at_eof = getattr(self._sr, 'at_eof', None)
        return bool(at_eof()) if at_eof is not None else False

    async def readuntil(self, sep: bytes) -> bytes:
        try:
            return await self._sr.readuntil(sep)
        except asyncio.IncompleteReadError as exc:
            raise IncompleteReadError(exc.partial) from exc

    async def readexactly(self, n: int) -> bytes:
        try:
            return await self._sr.readexactly(n)
        except asyncio.IncompleteReadError as exc:
            raise IncompleteReadError(exc.partial) from exc


class PrefixReader(AbstractReader):
    """An :class:`AbstractReader` that replays an already-read *prefix*.

    Connection detection peeks the first bytes of a stream to decide which
    protocol owns it; wrapping the underlying reader in a ``PrefixReader`` hands
    the *still-complete* stream to the protocol that claims it — the peeked bytes
    are served back first, then reads fall through to the underlying reader.

    Used by the decouple-connection-detection refactor so the dispatcher no
    longer consumes protocol-specific bytes on the connection's behalf.  The
    fast native ``readuntil`` / ``readexactly`` of the underlying reader are
    used once the prefix is drained, including the seam case where the separator
    straddles the prefix/underlying boundary.
    """

    def __init__(self, prefix: bytes, reader: AbstractReader) -> None:
        self._buf = bytearray(prefix)
        self._reader = reader

    async def read(self, n: int) -> bytes:
        if self._buf:
            chunk = bytes(self._buf[:n])
            del self._buf[:n]
            return chunk
        return await self._reader.read(n)

    async def readexactly(self, n: int) -> bytes:
        if len(self._buf) >= n:
            chunk = bytes(self._buf[:n])
            del self._buf[:n]
            return chunk
        head = bytes(self._buf)
        self._buf.clear()
        return head + await self._reader.readexactly(n - len(head))

    async def readuntil(self, sep: bytes) -> bytes:
        # Short-circuit: once the prefix is drained (the common keep-alive
        # case), delegate directly to the underlying reader's native,
        # buffered readuntil without any per-call overhead.
        if not self._buf:
            return await self._reader.readuntil(sep)
        idx = self._buf.find(sep)
        if idx != -1:
            end = idx + len(sep)
            chunk = bytes(self._buf[:end])
            del self._buf[:end]
            return chunk
        # Not wholly in the prefix: pull the rest with the underlying reader's
        # native readuntil, then re-resolve against the seam so a separator that
        # straddles the boundary is honoured and any over-read is pushed back.
        head = bytes(self._buf)
        self._buf.clear()
        combined = head + await self._reader.readuntil(sep)
        end = combined.find(sep) + len(sep)
        self._buf[:0] = combined[end:]          # push back over-read (usually none)
        return combined[:end]

    def at_eof(self) -> bool:
        return not self._buf and self._reader.at_eof()


# ---------------------------------------------------------------------------
# Fragment reassembly (RFC 6455 §5.4)
# ---------------------------------------------------------------------------

class FragmentAssembler:
    """Accumulates RFC 6455 fragmented frames and signals message completion.

    Feed each data/continuation frame via ``feed()``.  Returns
    ``(message_opcode, full_payload)`` when the final FIN=1 continuation
    arrives; returns ``None`` while still accumulating.

    Raises ``ProtocolError`` on violations:
    - CONTINUATION frame with no fragmentation in progress (§5.4)
    - New TEXT/BINARY opener while a fragmented message is open (§5.4)
    """

    def __init__(self) -> None:
        self._opcode: int | None = None
        self._buf: bytearray | None = None
        # Tracks the RSV1 bit of the message-opener frame (RFC 7692: only the
        # first frame of a compressed message carries RSV1=1; continuation
        # frames keep it clear).  Reported back from ``feed()`` so the caller
        # knows whether the assembled bytes need decompression.
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
            self._buf += payload
            if fin:
                result = (self._opcode, bytes(self._buf), self._compressed)
                self._opcode = None
                self._buf = None
                self._compressed = False
                return result
            return None
        else:
            # TEXT or BINARY opener
            if self.in_progress:
                raise ProtocolError(
                    'New data frame received while a fragmented message is in progress'
                )
            if fin:
                return (opcode, payload, rsv1)  # unfragmented — pass through immediately
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
    """Reads an HTTP/1.1 request body and emits a single ``http.request`` event.

    Body bytes are read lazily on the first ``__call__`` using the Content-Length
    or Transfer-Encoding header from ``scope``.  Subsequent calls return
    ``{'type': 'http.disconnect'}``.
    """

    _reader: AbstractReader  # narrows BaseRecipient._reader from AbstractReader | None

    def __init__(self, reader: AbstractReader, scope: dict,
                 *, body_timeout: float = 0.0,
                 deadline: ConnectionDeadline | None = None,
                 chunk_size: int | None = None):
        super().__init__(reader)
        self._scope = scope
        headers = scope['headers']
        if not isinstance(headers, Headers):
            headers = Headers(headers)
        te = headers.get(b'transfer-encoding', b'').strip().lower()
        cl = headers.get(b'content-length', b'')
        if te and te != b'chunked':
            raise NotImplementedError(
                f'Transfer-Encoding "{te.decode()}" is not supported.'
            )
        self._chunked = (te == b'chunked')
        # Remaining Content-Length bytes; counts down as the body streams.
        self._content_length = int(cl) if cl else None
        # P4: deliver a Content-Length body in fixed-size chunks instead of one
        # giant ``readexactly(content_length)`` allocation.  Falls back to the
        # ``BB_BODY_CHUNK_SIZE`` setting when not injected (direct-instantiation
        # tests pass it explicitly).
        if chunk_size is None:
            from ..env import get_settings as _get_settings  # noqa: PLC0415
            chunk_size = _get_settings().body_chunk_size
        self._chunk_size = chunk_size
        self._done = False
        # Sprint 17 Phase 3 — body-read deadline.  0 = disabled.  Applied
        # per ``_read_with_timeout`` call (i.e. per chunk for chunked
        # bodies, single window for Content-Length).  Mirrors nginx
        # ``client_body_timeout`` semantics: each read has the same bound.
        #
        # Sprint 23 — the deadline is rescheduled on the shared
        # :class:`ConnectionDeadline` rather than allocating a fresh
        # ``asyncio.wait_for`` Timeout per chunk.  Per-chunk semantics
        # preserved.
        self._body_timeout = body_timeout
        self._deadline = deadline

    async def _read_with_timeout(self, coro):
        """Run *coro* under the configured body_timeout, if any."""
        if self._body_timeout > 0 and self._deadline is not None:
            with self._deadline.guard(self._body_timeout):
                return await coro
        if self._body_timeout > 0:
            # Fallback for direct-instantiation tests that don't pass a
            # ConnectionDeadline.  Preserves per-call semantics; the
            # production hot path takes the deadline-guard branch above.
            return await asyncio.wait_for(coro, timeout=self._body_timeout)
        return await coro

    async def __call__(self) -> dict:
        if self._done:
            return {'type': ASGIEvent.HTTP_DISCONNECT}

        try:
            if self._chunked:
                size_line = await self._read_with_timeout(
                    self._reader.readuntil(b'\r\n'))
                chunk_size = _parse_chunk_size(size_line)
                if chunk_size == 0:
                    # RFC 9112 §7.1.2 — last-chunk is followed by an
                    # optional trailer-part and then a final CRLF.  Read
                    # lines until we hit the terminator.
                    while True:
                        line = await self._read_with_timeout(
                            self._reader.readuntil(b'\r\n'))
                        if line == b'\r\n':
                            break
                    self._done = True
                    return {'type': ASGIEvent.HTTP_REQUEST, 'body': b'', 'more_body': False}
                data = await self._read_with_timeout(
                    self._reader.read(chunk_size))
                await self._read_with_timeout(
                    self._reader.readuntil(b'\r\n'))        # consume CRLF after chunk data
                return {'type': ASGIEvent.HTTP_REQUEST, 'body': data, 'more_body': True}
            else:
                # P4: stream the Content-Length body in ``chunk_size`` slices so
                # a large upload is delivered as several ``http.request`` events
                # (``more_body: True`` until exhausted) rather than one giant
                # allocation.  ``readexactly`` keeps the exact-bytes contract —
                # a short body still raises IncompleteReadError below.
                if self._content_length:
                    n = min(self._content_length, self._chunk_size)
                    body = await self._read_with_timeout(
                        self._reader.readexactly(n))
                    self._content_length -= n
                    more = self._content_length > 0
                    if not more:
                        self._done = True
                    return {'type': ASGIEvent.HTTP_REQUEST, 'body': body,
                            'more_body': more}
                self._done = True
                return {'type': ASGIEvent.HTTP_REQUEST, 'body': b'', 'more_body': False}

        except (asyncio.TimeoutError, TimeoutError):
            # body_timeout exceeded — distinguish from EOF mid-body so
            # operators see the cap hit recorded (the request still
            # surfaces as HTTP_DISCONNECT to the ASGI app).
            log_cap_hit('body_timeout',
                        requested=self._body_timeout,
                        limit=self._body_timeout,
                        scope_path=self._scope.get('path') if self._scope else None,
                        protocol='http1')
            self._done = True
            return {'type': ASGIEvent.HTTP_DISCONNECT}
        except IncompleteReadError:
            # EOF mid-body — not a cap hit (peer disappeared).  Surface
            # disconnect so the handler can clean up; server closes on
            # return (no synthetic 408, see Sprint 17 plan note).
            self._done = True
            return {'type': ASGIEvent.HTTP_DISCONNECT}


class HTTP2Recipient(BaseRecipient):
    """Delivers HTTP/2 DATA frames as ASGI ``http.request`` events.

    The server loop feeds frames via ``put_DATAFrame()`` (non-blocking).
    The ASGI app calls ``__call__()`` which suspends until an event is available,
    hiding the concurrency from both sides.

    For GET-style requests (END_STREAM on HEADERS, no DATA frames), the caller
    invokes :meth:`mark_end_of_stream_on_headers` instead of pre-queuing an empty
    ``http.request`` event.  The Queue is then never allocated — the empty event
    is synthesized lazily in :meth:`__call__` only if the handler reads it.
    """

    def __init__(self, frame: FrameBase | None = None,
                 queue_depth: int = _HTTP2_STREAM_QUEUE_DEPTH):
        super().__init__(None)
        self._queue: asyncio.Queue | None = None
        self._queue_depth = queue_depth
        # When True, HEADERS carried END_STREAM — request has no body.
        # __call__() returns one empty http.request event without allocating a queue.
        self._end_of_stream_on_headers: bool = False
        # Set once the synthetic empty event has been delivered.
        self._initial_consumed: bool = False
        if isinstance(frame, Data):
            self.put_DATAFrame(frame)

    def _ensure_queue(self) -> asyncio.Queue:
        if self._queue is None:
            self._queue = asyncio.Queue(maxsize=self._queue_depth)
        return self._queue

    def mark_end_of_stream_on_headers(self) -> None:
        """Mark this stream as ended on HEADERS (no body to deliver).

        Replaces ``put_event({type: http.request, body: b'', more_body: False})``
        with a flag — saves one ``asyncio.Queue`` allocation per body-less request.
        """
        self._end_of_stream_on_headers = True

    def make_event(self, frame: Data) -> dict:
        return {
            'type': ASGIEvent.HTTP_REQUEST,
            'body': frame.payload,
            'more_body': False if frame.end_stream else True,
        }

    def put_DATAFrame(self, frame: Data) -> bool:
        """Enqueue a DATA frame event. Returns False if the queue is full."""
        try:
            self._ensure_queue().put_nowait(self.make_event(frame))
            return True
        except asyncio.QueueFull:
            logger.warning('HTTP2Recipient queue full on stream — dropping DATA frame')
            log_cap_hit('stream_queue_depth',
                        requested=self._queue_depth + 1,
                        limit=self._queue_depth,
                        protocol='http2')
            return False

    def put_event(self, event: dict) -> bool:
        """Enqueue a pre-built event dict. Returns False if the queue is full."""
        try:
            self._ensure_queue().put_nowait(event)
            return True
        except asyncio.QueueFull:
            logger.warning('HTTP2Recipient queue full on stream — dropping event %r', event.get('type'))
            log_cap_hit('stream_queue_depth',
                        requested=self._queue_depth + 1,
                        limit=self._queue_depth,
                        protocol='http2')
            return False

    def put_disconnect(self) -> None:
        """Unblock a waiting __call__() with an http.disconnect event.

        Skipped when end-of-stream-on-headers has been delivered and no queue
        was ever created — no consumer can be waiting.
        """
        if (self._queue is None
                and self._end_of_stream_on_headers
                and self._initial_consumed):
            return
        try:
            self._ensure_queue().put_nowait({'type': ASGIEvent.HTTP_DISCONNECT})
        except asyncio.QueueFull:
            # If the queue is completely full the app task is hopelessly behind;
            # TaskGroup cancellation will clean up the stream regardless.
            logger.warning('HTTP2Recipient: could not deliver http.disconnect — queue full')

    async def __call__(self) -> dict:
        # Fast path: GET with END_STREAM on HEADERS and no body — synthesize the
        # empty http.request event without allocating a queue.
        if (self._end_of_stream_on_headers
                and not self._initial_consumed
                and self._queue is None):
            self._initial_consumed = True
            return {'type': ASGIEvent.HTTP_REQUEST, 'body': b'', 'more_body': False}
        return await self._ensure_queue().get()


class WebSocketRecipient(BaseRecipient):
    """Reads WebSocket frames and emits ASGI ``websocket.*`` events.

    First call returns ``{'type': 'websocket.connect'}``.  Subsequent calls
    read the next frame from the transport:
      - Text frame   → ``{'type': 'websocket.receive', 'text': ..., 'bytes': None}``
      - Binary frame → ``{'type': 'websocket.receive', 'text': None, 'bytes': ...}``
      - Close frame  → ``{'type': 'websocket.disconnect', 'code': 1000}``
      - Ping frame   → sends Pong immediately, then reads the next frame
      - Pong frame   → silently dropped, reads the next frame

    Ping/pong handling requires write access to the transport, so the raw
    writer is stored alongside the reader.
    """

    # Hard cap on the declared payload length of a single inbound
    # WebSocket frame.  RFC 6455 §5.2 allows up to 2**63 - 1, which an
    # adversary post-handshake can use to OOM the server before any
    # body bytes arrive (``read_payload`` would attempt to buffer the
    # full declared length).  ``MESSAGE_TOO_BIG`` (1009) is the
    # RFC 6455 §7.4.1 close code.
    #
    # Default: 64 MiB — large enough to pass the Autobahn|Testsuite
    # 9.x large-message cases (up to 9.1.6 = 64 MiB text) while still
    # bounding per-connection memory.  Sprint 39 (v0.35.0) introduced
    # this cap at 1 MiB; Sprint 43 raised the default to 64 MiB and
    # exposed it via ``BB_WS_MAX_FRAME_PAYLOAD`` after the conformance
    # CI lane discovered the 1 MiB cap was regressing Autobahn 9.x.
    # Override per-deployment via ``BB_WS_MAX_FRAME_PAYLOAD`` for
    # stricter (or looser) exposure than the default.
    _MAX_FRAME_PAYLOAD: int = 64 * 1024 * 1024

    def __init__(self, reader: AbstractReader, writer: AbstractWriter, *,
                 require_masked: bool = True,
                 dispatcher: EventDispatcher | None = None,
                 scope: dict | None = None,
                 ws_queue_depth: int = _WS_EVENT_QUEUE_DEPTH,
                 decompressor=None,
                 max_frame_payload: int | None = None):
        super().__init__(reader)
        self._writer = writer
        self._connect_sent = False
        self._assembler = FragmentAssembler()
        # Resolution order for the cap:
        #  1. explicit ``max_frame_payload=`` constructor arg (tests + power users)
        #  2. ``BB_WS_MAX_FRAME_PAYLOAD`` env var via Settings
        #  3. class default (``_MAX_FRAME_PAYLOAD``)
        # Late import keeps ``recipient`` importable without bringing in the
        # full settings stack — useful for tests that drive the recipient
        # directly without a Settings populated.
        if max_frame_payload is not None:
            self._max_frame_payload: int = max_frame_payload
        else:
            try:
                from ..env import get_settings  # noqa: PLC0415
                self._max_frame_payload = get_settings().ws_max_frame_payload
            except Exception:
                self._max_frame_payload = self._MAX_FRAME_PAYLOAD
        # Server-side: client frames MUST be masked (RFC 6455 §5.1).  Client-side:
        # server frames MUST NOT be masked, so the recipient must not raise when
        # they aren't.  When ``require_masked`` is False, outgoing PONG frames
        # generated by this recipient also need masking, since masking is
        # symmetric: whoever requires masking *in* must not mask *out*.
        self._require_masked = require_masked
        self._dispatcher = dispatcher
        self._scope = scope
        self._ws_queue_depth = ws_queue_depth
        self._event_queue: asyncio.Queue | None = None
        self._reader_task: asyncio.Task | None = None
        # When permessage-deflate is negotiated, an
        # :class:`InboundDecompressor` is supplied here.  None means
        # compression is disabled for this connection and any inbound RSV1=1
        # frame is treated as a protocol violation (handled by the read loop).
        self._decompressor = decompressor

    async def _read_loop(self) -> None:
        """Eagerly read frames from the wire, emit events, and queue ASGI events."""
        assert self._event_queue is not None
        _CONTROL_OPS = (WSOpcode.CLOSE, WSOpcode.PING, WSOpcode.PONG)
        try:
            while True:
                h = await read_frame_header(self._reader)

                # RFC 6455 §5.5 — control frames MUST have payload ≤125 and
                # MUST NOT be fragmented.  Reject without reading the body.
                if h.opcode in _CONTROL_OPS:
                    if not h.fin:
                        raise ProtocolError('fragmented control frame')
                    if h.length > 125:
                        raise ProtocolError(
                            f'control frame payload {h.length} > 125')

                # RFC 6455 §5.2 — reserved RSV bits MUST be 0 unless an
                # extension defining them was negotiated in the handshake.
                # RSV1 is owned by permessage-deflate (RFC 7692); RSV2 / RSV3
                # are not defined by any extension we negotiate, so they are
                # always a protocol error.  RSV1 on a control frame is
                # likewise always a violation per RFC 7692 §6.
                if h.rsv2 or h.rsv3:
                    raise ProtocolError(
                        f'RSV2/RSV3 set without negotiated extension '
                        f'(rsv2={h.rsv2} rsv3={h.rsv3})')
                if h.rsv1 and (self._decompressor is None or h.opcode in _CONTROL_OPS):
                    raise ProtocolError(
                        f'RSV1 set on frame (opcode={h.opcode}) without '
                        f'negotiated permessage-deflate')

                # Hard cap on declared payload length.  ``h.length`` is
                # the wire indicator (0–125, 126, or 127); the resolved
                # extended length is read inside read_payload, which
                # raises FramePayloadTooLarge before any body bytes are
                # read off the wire.  Defends against post-handshake
                # OOM where the peer advertises a 2**63 - 1 payload.
                try:
                    payload = await read_payload(
                        self._reader, h.masked, h.length,
                        max_length=self._max_frame_payload)
                except FramePayloadTooLarge as exc:
                    log_cap_hit('ws_max_frame_payload',
                                requested=exc.declared,
                                limit=self._max_frame_payload,
                                scope_path=self._scope.get('path') if self._scope else None,
                                protocol='ws')
                    raise ProtocolError(
                        str(exc),
                        close_code=WSCloseCode.MESSAGE_TOO_BIG,
                    ) from exc

                if self._require_masked and not h.masked:
                    raise ProtocolError('unmasked client frame')

                match h.opcode:
                    case WSOpcode.TEXT | WSOpcode.BINARY | WSOpcode.CONTINUATION:
                        done = await self._handle_data_frame(
                            h.opcode, payload, h.fin, h.rsv1)
                        if not done:
                            continue
                    case WSOpcode.CLOSE | WSOpcode.PING | WSOpcode.PONG:
                        done = await self._handle_control_frame(h.opcode, payload)
                        if done:
                            return
                    case _:
                        await self._handle_unknown_opcode()
                        return

        except (asyncio.IncompleteReadError, IncompleteReadError):
            await self._emit_disconnected(WSCloseCode.ABNORMAL)
            await self._event_queue.put({'type': ASGIEvent.WS_DISCONNECT, 'code': WSCloseCode.ABNORMAL})
        except ProtocolError as exc:
            close = encode_frame(
                exc.close_code.to_bytes(2, 'big'),
                opcode=WSOpcode.CLOSE,
                mask=not self._require_masked,
            )
            try:
                await self._writer.write(close)
            except Exception:
                pass  # best-effort CLOSE frame; the socket may already be gone.
            await self._emit_disconnected(exc.close_code)
            # Surface the violation on the next app-side receive() (matches
            # the legacy contract that any exception in the read loop is
            # raised back to the app); the close frame has already gone out.
            await self._event_queue.put(exc)
        except Exception as exc:
            close = encode_frame(
                (1011).to_bytes(2, 'big'),  # INTERNAL_ERROR
                opcode=WSOpcode.CLOSE,
                mask=not self._require_masked,
            )
            try:
                await self._writer.write(close)
            except Exception:
                pass  # best-effort CLOSE frame; the socket may already be gone.
            await self._event_queue.put(exc)

    async def _handle_data_frame(self, opcode, payload: bytes, fin: bool,
                                 rsv1: bool = False) -> bool:
        """Handle TEXT/BINARY/CONTINUATION frame; returns True if a complete message was queued."""
        assert self._event_queue is not None
        result = self._assembler.feed(opcode, payload, fin, rsv1)
        if result is None:
            return False
        msg_opcode, full_payload, compressed = result
        if compressed:
            assert self._decompressor is not None  # frame loop enforced this
            try:
                full_payload = self._decompressor.decompress(full_payload)
            except Exception as exc:
                # RFC 7692 §7.1 — a payload that fails to decompress is a
                # connection error.  Treat as PROTOCOL_ERROR (1002).
                raise ProtocolError(
                    f'permessage-deflate decompression failed: {exc}',
                    close_code=1002,
                ) from exc
        if msg_opcode == WSOpcode.TEXT:
            try:
                text = full_payload.decode('utf-8')
            except UnicodeDecodeError as e:
                # RFC 6455 §8.1 — invalid UTF-8 in a TEXT message MUST be
                # treated as a CLOSE with status code 1007.
                raise ProtocolError(f'invalid UTF-8 in TEXT message: {e}',
                                    close_code=1007)
            asgi_event = {
                'type': ASGIEvent.WS_RECEIVE,
                'text': text,
                'bytes': None,
            }
        else:
            asgi_event = {
                'type': ASGIEvent.WS_RECEIVE,
                'text': None,
                'bytes': full_payload,
            }
        if self._dispatcher is not None and self._scope is not None:
            await self._dispatcher.emit(Event(
                'websocket_message',
                detail={
                    'scope': self._scope,
                    'text': asgi_event['text'],
                    'bytes': asgi_event['bytes'],
                },
            ))
        await self._event_queue.put(asgi_event)
        return True

    async def _handle_control_frame(self, opcode, payload: bytes) -> bool:
        """Handle CLOSE/PING/PONG frame; returns True if the connection should close."""
        assert self._event_queue is not None
        if opcode == WSOpcode.CLOSE:
            # RFC 6455 §5.5.1 — when an endpoint receives a Close frame and
            # has not yet sent one, it MUST send a Close frame in response,
            # echoing the peer's status code if present.  Validate the code
            # and the reason text first; on any violation, send 1002 instead.
            code, reason_ok = _parse_close_payload(payload)
            echo_code = code if reason_ok else WSCloseCode.PROTOCOL_ERROR
            event_code = code if reason_ok else WSCloseCode.PROTOCOL_ERROR
            close = encode_frame(
                echo_code.to_bytes(2, 'big'),
                opcode=WSOpcode.CLOSE,
                mask=not self._require_masked,
            )
            try:
                await self._writer.write(close)
            except Exception:
                pass  # best-effort CLOSE frame; the socket may already be gone.
            await self._emit_disconnected(event_code)
            await self._event_queue.put(
                {'type': ASGIEvent.WS_DISCONNECT, 'code': event_code})
            return True
        if opcode == WSOpcode.PING:
            # RFC 6455 §5.5 — control-frame payload MUST be ≤125 bytes; the
            # frame-header reader catches that case before we get here.
            pong = encode_frame(payload, opcode=WSOpcode.PONG, mask=not self._require_masked)
            await self._writer.write(pong)
        # PONG: unsolicited pong — silently drop
        return False

    async def _handle_unknown_opcode(self) -> None:
        """Send a CLOSE frame and queue a disconnect event for an unknown opcode."""
        assert self._event_queue is not None
        close = encode_frame(
            WSCloseCode.PROTOCOL_ERROR.to_bytes(2, 'big'), opcode=WSOpcode.CLOSE)
        try:
            await self._writer.write(close)
        except Exception:
            pass  # best-effort CLOSE frame; the socket may already be gone.
        await self._emit_disconnected(WSCloseCode.PROTOCOL_ERROR)
        await self._event_queue.put(
            {'type': ASGIEvent.WS_DISCONNECT, 'code': WSCloseCode.PROTOCOL_ERROR})

    async def _emit_disconnected(self, code: int) -> None:
        """Emit websocket_disconnected exactly once per connection."""
        if (self._dispatcher is not None and self._scope is not None
                and not self._scope.get('_ws_disconnected')):
            self._scope['_ws_disconnected'] = True
            await self._dispatcher.emit(Event(
                'websocket_disconnected',
                detail={
                    'scope':         self._scope,
                    'connection_id': self._scope.get('_connection_id', ''),
                    'client_ip':     self._scope['client'][0] if self._scope.get('client') else '',
                    'path':          self._scope.get('path', ''),
                    'code':          code,
                },
            ))

    def _ensure_reader_started(self) -> None:
        if self._event_queue is None:
            self._event_queue = asyncio.Queue(maxsize=self._ws_queue_depth)
            self._reader_task = asyncio.create_task(self._read_loop())

    async def __call__(self) -> dict:
        if not self._connect_sent:
            self._connect_sent = True
            self._ensure_reader_started()
            return {'type': ASGIEvent.WS_CONNECT}
        self._ensure_reader_started()
        item = await self._event_queue.get()  # type: ignore[union-attr]
        if isinstance(item, Exception):
            raise item
        return item


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

class RecipientFactory:
    """Creates the appropriate ``BaseRecipient`` for the given protocol.

    All methods that need a reader accept a raw asyncio-compatible stream reader
    and wrap it in ``AsyncioReader`` internally.
    """

    @staticmethod
    def http1(reader, scope: dict, *,
              body_timeout: float = 0.0,
              deadline: ConnectionDeadline | None = None) -> HTTP1Recipient:
        if not isinstance(reader, AbstractReader):
            reader = AsyncioReader(reader)
        return HTTP1Recipient(reader, scope, body_timeout=body_timeout,
                              deadline=deadline)

    @staticmethod
    def http2(frame: FrameBase | None = None,
              queue_depth: int = _HTTP2_STREAM_QUEUE_DEPTH) -> HTTP2Recipient:
        return HTTP2Recipient(frame, queue_depth=queue_depth)

    @staticmethod
    def websocket(reader, writer, *,
                  dispatcher: EventDispatcher | None = None,
                  scope: dict | None = None,
                  ws_queue_depth: int = _WS_EVENT_QUEUE_DEPTH,
                  decompressor=None) -> WebSocketRecipient:
        if not isinstance(reader, AbstractReader):
            reader = AsyncioReader(reader)
        if not isinstance(writer, AbstractWriter):
            writer = AsyncioWriter(writer)
        return WebSocketRecipient(reader, writer, dispatcher=dispatcher, scope=scope,
                                  ws_queue_depth=ws_queue_depth,
                                  decompressor=decompressor)