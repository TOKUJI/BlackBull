"""The send side: what a handler produced, as protocol bytes.

An [`AbstractWriter`][blackbull.server.sender.AbstractWriter] is a
protocol-agnostic async byte sink; a
[`BaseSender`][blackbull.server.sender.BaseSender] turns a bytes body, an ASGI
send event, or a [`NativeResponse`][blackbull.native.NativeResponse] into wire
format, one subclass per protocol —
[`HTTP1Sender`][blackbull.server.sender.HTTP1Sender],
[`HTTP2Sender`][blackbull.server.sender.HTTP2Sender] and
[`WebSocketSender`][blackbull.server.sender.WebSocketSender].
[`SenderFactory`][blackbull.server.sender.SenderFactory] builds the right one
over a raw asyncio stream writer.

A sender never picks between joining its parts and writing them vectored: it
hands them to ``BaseSender._write_many`` and a size gate decides.  The
Internals page states that threshold, and what anything backing
[`AsyncioWriter`][blackbull.server.sender.AsyncioWriter] therefore owes it.
"""
import asyncio
import os
import time
from abc import ABC, abstractmethod
from http import HTTPStatus
from inspect import iscoroutinefunction
from email.utils import formatdate
from itertools import chain
from typing import NoReturn

from ..protocol import hpack_fastpath
from ..protocol.frame_types import (FrameTypes, HeaderFrameFlags, DataFrameFlags,
                                    FrameBase, PseudoHeaders,
                                    DEFAULT_INITIAL_WINDOW_SIZE, DEFAULT_MAX_FRAME_SIZE)
from .cap_log import log_cap_hit
from .constants import WSCloseCode
from .deadline import WriteDeadline
from .ws_codec import WSOpcode, encode_frame, encode_frame_header
import logging
from ..asgi import (
    ASGIEvent,
    ASGISendEvent,
    WebSocketAcceptEvent,
    WebSocketCloseEvent,
    WebSocketSendEvent,
)
from ..headers import Headers, HeaderList
from ..native import NativeResponse, NativeWSMessage

from ..logger import debug_gate  # noqa: E402
logger = logging.getLogger(__name__)
#: Read once at import: a disabled ``logger.debug`` on a per-request path
#: costs 24 executed instructions to emit nothing.  Same bargain as
#: ``@log`` — see [`blackbull.logger.debug_gate`][blackbull.logger.debug_gate].
_DEBUG = debug_gate(logger)


_CRLF = b'\r\n'

# Fallback chunk size when ``sendfile`` isn't supported by the transport
# (TLS, mocked tests).  Matches the static middleware's ``_CHUNK`` so
# memory-peak guarantees stay consistent across paths.
_PATHSEND_FALLBACK_CHUNK = 64 * 1024

# Why a megabyte: docs/about/internals.md §Send-path invariant.
_SENDFILE_CHUNK = 1024 * 1024

# The join-vs-vectored gate; why 32 KiB: docs/about/internals.md §Send-path
# invariant.  Breakeven measured on a drained socketpair (selector transport):
# join wins ≤ 16 KiB, vectored wins ≥ 64 KiB, and HttpArena's 17 KiB static
# lanes regressed under ``writelines``.  Deliberately not a Settings knob — no
# configuration surface without deployment data.
_VECTORED_JOIN_THRESHOLD = 32 * 1024


_STATUS_LINES: dict[HTTPStatus, bytes] = {
    s: f'HTTP/1.1 {s} {s.phrase}'.encode() + _CRLF for s in HTTPStatus
}


def _status_line(status) -> bytes:
    """The full ``HTTP/1.1 <code> <phrase>\\r\\n`` line for *status*."""
    line = _STATUS_LINES.get(status)
    if line is None:
        # A code IANA has not registered has no ``phrase``, so it renders with
        # an empty reason phrase — legal: RFC 9112 §4 makes it optional.
        phrase = getattr(status, 'phrase', '')
        return f'HTTP/1.1 {int(status)} {phrase}'.encode() + _CRLF
    return line


_CONTENT_LENGTH_CACHE_MAX = 8192

_CONTENT_LENGTHS: tuple[bytes, ...] = tuple(
    str(n).encode() for n in range(_CONTENT_LENGTH_CACHE_MAX + 1)
)


def _content_length_bytes(n: int) -> bytes:
    """Decimal ASCII for *n*, from the table when it is small enough."""
    if 0 <= n <= _CONTENT_LENGTH_CACHE_MAX:
        return _CONTENT_LENGTHS[n]
    return str(n).encode()


# RFC 7231 Date header is whole-second resolution, so re-formatting it
# per response is wasted work — email.utils.formatdate shows ~2.6% of
# CPU on a B2r profile.  Cache for the current integer second.
_HTTP_DATE_TS: int = 0
_HTTP_DATE: bytes = b''


def _http_date() -> bytes:
    global _HTTP_DATE_TS, _HTTP_DATE
    now = int(time.time())
    if now != _HTTP_DATE_TS:
        _HTTP_DATE = formatdate(timeval=now, localtime=False, usegmt=True).encode('ascii')
        _HTTP_DATE_TS = now
    return _HTTP_DATE


def _is_informational(status) -> bool:
    """True for a 1xx status — a provisional response, not the final one.

    An interim response shares the sender with the final response that must
    still follow it, so it neither completes the exchange nor commits a
    status, and it carries no content framing (RFC 9110 §8.6, §15.2).
    """
    return int(status) < 200


def _parse_content_length(headers: Headers) -> int | None:
    """Return one unambiguous Content-Length value from response headers."""
    values: list[int] = []
    for _name, raw in headers.getlist(b'content-length'):
        for member in raw.split(b','):
            value = member.strip(b' \t')
            if not value or not value.isdigit():
                raise ValueError('invalid Content-Length response header')
            values.append(int(value))
    if not values:
        return None
    if any(value != values[0] for value in values[1:]):
        raise ValueError('conflicting Content-Length response headers')
    return values[0]


def _has_header(items, name: bytes) -> bool:
    """Case-insensitive membership check over ``(key, value)`` tuples.

    HTTP/2 field names are lowercase ASCII per RFC 9113 §8.2.1, but the
    ASGI app may still hand us ``b'Date'`` or ``b'DATE'`` — its problem
    to surface, ours to honour.  Used by HTTP2Sender to avoid
    duplicating the auto-emitted ``date`` header when the app already
    set one.
    """
    needle = name.lower()
    return any(k.lower() == needle for k, _ in items)


# The two builders below must stay byte-for-byte equivalent to the frame-object
# path they replace — ``protocol.frame_types.Headers.save()``, not this module's
# field-collection ``Headers`` — including how the shared HPACK dynamic table
# evolves; ``tests/conformance/http2/test_headers_fastpath_builder.py`` asserts
# both.  ``status_fast_bytes`` is static-indexed, so it never touches that table
# (RFC 7541 §6.1).
#
# Assumption: the encoded block fits one frame (END_HEADERS always set).
# BlackBull does not split outbound HEADERS across CONTINUATION; if that
# ever changes these builders need a fallback.

def build_response_headers(encoder, stream_id: int, status,
                           headers, *, end_stream: bool) -> bytes:
    """Encode a response HEADERS frame (carrying ``:status``) to wire bytes.

    Injects a ``date`` header when the app did not supply one, mirroring the
    ``Headers.save()`` send path.  ``status`` may be an ``HTTPStatus``, an
    ``int``, or a ``str`` — it is normalised via ``str()`` exactly as the
    object path does.
    """
    if _has_header(headers, b'date'):
        fields = headers
    else:
        fields = (*headers, (b'date', _http_date()))

    fast = hpack_fastpath.status_fast_bytes(str(status))
    if fast is not None:
        payload = fast + encoder.encode(fields)
    else:
        payload = encoder.encode(
            chain(((PseudoHeaders.STATUS, str(status)),), fields))

    flags = HeaderFrameFlags.END_HEADERS.value
    if end_stream:
        flags |= HeaderFrameFlags.END_STREAM.value
    return (len(payload).to_bytes(3, 'big') + FrameTypes.HEADERS.value
            + flags.to_bytes(1, 'big') + stream_id.to_bytes(4, 'big') + payload)


def build_trailers(encoder, stream_id: int, headers) -> bytes:
    """Encode a trailers HEADERS frame (END_HEADERS | END_STREAM, no
    pseudo-headers) to wire bytes.

    This is the basis for the gRPC ``grpc-status`` trailers path — a unary
    RPC response carries a second HEADERS frame with regular fields only.
    """
    payload = encoder.encode(headers)
    flags = HeaderFrameFlags.END_HEADERS.value | HeaderFrameFlags.END_STREAM.value
    return (len(payload).to_bytes(3, 'big') + FrameTypes.HEADERS.value
            + flags.to_bytes(1, 'big') + stream_id.to_bytes(4, 'big') + payload)


class AbstractWriter(ABC):
    """Protocol-agnostic async byte-sink.

    ``write()`` is the single responsibility: deliver bytes and ensure they
    are flushed.  Backpressure, buffering, and draining are implementation
    details of each concrete subclass — callers never call ``drain()`` directly.

    Implementors wrap a concrete transport (asyncio.StreamWriter, trio
    MemorySendStream, curio socket, …).  ``BaseSender`` only depends on this
    interface, so switching the async runtime requires only a new subclass here.
    """

    def peer_is_gone(self) -> bool:
        """True when the transport has already recorded the loss.

        Asked *before* a write, because the exception the guard below catches
        can arrive arbitrarily later: ``connection_lost`` is delivered through
        ``call_soon``, so between the transport recording the loss and the
        protocol learning of it there is a window in which ``write()`` drops
        silently and ``drain()`` returns without raising.  Every write in that
        window is one asyncio counts and warns about.
        """
        return False

    #: Set once the peer is known to be gone.  It lives on the writer rather
    #: than on the sender because *the connection* is what died.  HTTP/2 builds
    #: one sender per stream over one writer (``HTTP2Actor.make_sender``), so a
    #: per-sender flag has every stream rediscover the same dead socket by
    #: writing into it — asyncio drops those writes and logs a warning for each
    #: one past its threshold of 5.
    peer_gone: bool = False

    @abstractmethod
    async def write(self, data: bytes) -> None:
        """Write *data* to the transport and ensure it is flushed."""

    async def writelines(self, parts) -> None:
        """Write multiple byte segments without joining them in user space.

        Default joins-and-writes so subclasses can opt out.  Override in
        transports whose ``writelines`` does vectored I/O (``writev`` /
        ``sendmsg``) to skip the full-body memcpy on the static-file
        cache-hit path.
        """
        await self.write(b''.join(parts))

    async def close(self) -> None:
        """Close the underlying transport. Default: no-op."""

    async def sendfile(self, file, offset: int, count: int) -> int:
        """Send up to *count* bytes from *file* starting at *offset*.

        Default implementation raises ``NotImplementedError`` so callers
        can detect lack of support and fall back to a read+write loop.
        Concrete subclasses opt in when the underlying transport
        supports a zero-copy path (Linux ``sendfile(2)`` /
        ``loop.sendfile``).

        Used by the static-file middleware via the
        ``http.response.pathsend`` ASGI extension.
        """
        raise NotImplementedError(
            'sendfile is not supported by this writer')


class AsyncioWriter(AbstractWriter):
    """Adapts an asyncio-compatible stream to ``AbstractWriter``.

    The constructor accepts any object that exposes ``write(bytes)`` (sync)
    and ``drain()`` (async) — the asyncio StreamWriter API — so that test
    doubles such as ``MagicMock`` can be injected without ceremony.

    ``drain()`` is called inside ``write()`` so the asyncio backpressure
    mechanism is handled transparently and ``BaseSender`` stays runtime-agnostic.

    ``write_timeout`` (seconds, ``0`` = disabled) bounds the time spent in
    ``drain()`` waiting for the kernel send buffer to flush — the slow-read
    shape of slowloris, where a peer reading at 1 byte/sec blocks the drain on
    a TCP window that never reopens.  On timeout the transport is closed and
    ``ConnectionResetError`` raised, so the sender treats it as a peer-side
    reset.  The bound rides the shared ``ConnectionDeadline`` scanner,
    whose ``BB_DEADLINE_TICK_MS`` granularity is the slop on when it fires.
    """

    def __init__(self, stream_writer, write_timeout: float = 0.0,
                 cap_name: str = 'write_timeout',
                 protocol: str | None = None):
        if not (hasattr(stream_writer, 'write') and hasattr(stream_writer, 'drain')):
            raise TypeError(
                f"AsyncioWriter requires an object with write() and drain(), "
                f"got {type(stream_writer)}"
            )
        self._sw = stream_writer
        # A ``MagicMock`` fabricates ``.transport.is_closing`` on demand, which
        # is why ``peer_is_gone`` compares the result against ``True`` by
        # identity rather than truthiness — a mock must not close every write.
        _transport = getattr(stream_writer, 'transport', None)
        self._is_closing = getattr(_transport, 'is_closing', None)
        self._write_timeout = write_timeout
        self._cap_name = cap_name
        self._protocol = protocol
        self._deadline = (WriteDeadline(write_timeout)
                          if write_timeout > 0 else None)
        # Same mock hazard: the capability check has to be something a
        # fabricated attribute cannot accidentally pass.
        linger = getattr(stream_writer, 'linger_close', None)
        self._linger = (linger
                        if linger is not None and iscoroutinefunction(linger)
                        else None)

    async def _drain_with_timeout(self) -> None:
        """Drain the underlying StreamWriter, bounded by ``_write_timeout``.

        On timeout, close the transport (so the FD/connection slot is
        reclaimed from a slow-read peer or dead TCP route) and surface a
        ``ConnectionResetError`` so the sender's existing peer-disconnect
        handling runs uniformly.  When no timeout is configured this is a
        plain ``drain()``.
        """
        dl = self._deadline
        if dl is None:
            await self._sw.drain()
            return
        try:
            with dl:
                await self._sw.drain()
        except TimeoutError:
            self._fail_write_timeout()

    def _fail_write_timeout(self) -> NoReturn:
        """Tear the connection down after a write bound expired, and raise.

        Shared by every bounded write — the drain and each ``sendfile``
        chunk — so a slow-read peer meets the same fate whichever path it
        stalls.  Never returns.
        """
        logger.warning(
            'write timeout (%.1fs) exceeded — closing connection',
            self._write_timeout)
        if self._cap_name == 'write_timeout':
            log_cap_hit('write_timeout',
                        requested=self._write_timeout,
                        limit=self._write_timeout,
                        protocol=self._protocol)
        else:
            log_cap_hit(self._cap_name,
                        requested=self._write_timeout,
                        limit=self._write_timeout,
                        protocol=self._protocol)
        try:
            self._sw.close()
        except Exception as close_exc:
            # The transport may already be half-broken (SSL aborted, FD reaped
            # by a sibling task); swallowing lets the ConnectionResetError
            # below still reach the uniform peer-disconnect handling.
            if _DEBUG:
                logger.debug(
                    'write timeout: transport.close() also failed (%s) — '
                    'continuing with ConnectionResetError', close_exc)
        raise ConnectionResetError(
            f'write timeout after {self._write_timeout:.1f}s'
        ) from None

    async def write(self, data: bytes) -> None:
        self._sw.write(data)
        await self._drain_with_timeout()

    async def writelines(self, parts) -> None:
        """Vectored write via the underlying StreamWriter.

        ``asyncio.StreamWriter.writelines`` hands the iterable to
        ``transport.writelines``, which on the selector transport uses
        ``socket.sendmsg(iovec, …)`` for the immediate-send case and on
        uvloop is implemented as a real vectored write.  Either way the
        body bytes never get copied into a fresh ``bytes`` object before
        the syscall.
        """
        self._sw.writelines(parts)
        await self._drain_with_timeout()

    def peer_is_gone(self) -> bool:
        check = self._is_closing
        if check is None:
            return False  # No transport to ask; the exception path decides.
        try:
            return check() is True
        except Exception:  # noqa: BLE001 - a diagnostic must never break a write
            return False

    async def close(self) -> None:
        # We deliberately do NOT await ``wait_closed()``: it costs 1-3 event-loop
        # turns per connection under burst-keepalive (HttpArena ``static``,
        # c=4096), and thousands of simultaneous closes multiply that into a
        # multi-second drain.  Safe because ``write()`` above already drained.
        if self._linger is not None:
            await self._linger()
            return
        self._sw.close()

    async def sendfile(self, file, offset: int, count: int) -> int:
        """Zero-copy ``loop.sendfile`` against the underlying transport, in
        bounded chunks.

        Raises ``NotImplementedError`` (propagated from the loop) when the
        transport is SSL — TLS framing happens in user-space, so the kernel
        cannot see the plaintext to copy.  Callers must catch that and fall
        back to a read+write loop.  Support is a property of the transport, so
        it is decided on the first chunk: a later chunk cannot discover that
        sendfile was unavailable all along.

        Drains any pending writes first, under the write bound like every
        other drain, so buffered headers precede the file bytes in wire order.
        The chunking is what gives ``BB_WRITE_TIMEOUT`` somewhere to re-arm;
        the Internals page sizes it.  Returns the octets actually sent, short
        of *count* only when the peer stopped accepting.
        """
        await self._drain_with_timeout()
        loop = asyncio.get_running_loop()
        dl = self._deadline
        sent = 0
        while sent < count:
            want = min(_SENDFILE_CHUNK, count - sent)
            if dl is None:
                n = await loop.sendfile(
                    self._sw.transport, file, offset + sent, want)
            else:
                try:
                    with dl:
                        n = await loop.sendfile(
                            self._sw.transport, file, offset + sent, want)
                except TimeoutError:
                    self._fail_write_timeout()
            if not n:
                # Zero octets is the peer gone, not a chunk to retry.
                break
            sent += n
        return sent


# What the senders accept beyond the ASGI send contract is widened privately
# here rather than in ``ASGISendEvent``: an app or middleware author holding an
# ``ASGISendCallable`` must not be told that sending a bare byte string is
# legal, because through the app-facing channel it is not.
_SenderEvent = ASGISendEvent
_SenderBody = _SenderEvent | bytes | NativeResponse
_WSSenderEvent = WebSocketSendEvent | WebSocketCloseEvent | WebSocketAcceptEvent


class BaseSender(ABC):
    """Abstract base for ASGI-event → wire-format senders.

    ``__call__`` accepts either:
      - ``bytes`` body + optional ``status`` and ``headers``: the sender builds
        and sends the full protocol response (start + body) in one call.
      - A protocol-specific event dict: dispatched to the appropriate handler.

    The actual byte transport is hidden behind ``AbstractWriter`` so the sender
    logic is decoupled from asyncio internals.
    """

    # Senders are allocated per stream / per request on the hot path, and an
    # ABC already provides ``__slots__ = ()``, so declaring slots here drops
    # the per-instance ``__dict__``.  Every subclass must extend the tuple with
    # its own attributes or pay the ``__dict__`` back.
    __slots__ = ('_writer', '_closed')

    def __init__(self, writer: AbstractWriter):
        self._writer = writer
        self._closed = False

    def mark_client_gone(self) -> None:
        """The peer is gone — drop further writes instead of raising.

        The actor calls this when a read fails in a way that proves the
        connection is dead (an ``IncompleteReadError`` that escaped the body
        reader), so the response it may still be mid-way through writing dies
        quietly rather than as a broken-pipe traceback.

        A method and not an ``http.disconnect`` down the send channel: that
        would widen every sender's public event union to admit a message no
        application or middleware may legally send, teaching the wrong
        contract to anyone who reads the signature.  ``http.disconnect``
        stays the app-facing spelling on ``receive()``, the direction ASGI
        defines it in.
        """
        self._closed = True

    @abstractmethod
    async def __call__(self, body: _SenderBody,
                       status: HTTPStatus = HTTPStatus.OK,
                       headers: HeaderList = []): pass

    async def _guarded_write(self, write_fn, arg) -> None:
        """Run *write_fn(arg)* tolerant of peer-closed transports.

        Once a write hits ``ConnectionResetError`` / ``BrokenPipeError`` / SSL
        EOF the sender marks itself closed and subsequent writes silently drop;
        unguarded, those surface as tracebacks under sustained load.  The
        discovery is published to [`AbstractWriter.peer_gone`][AbstractWriter.peer_gone] so that no
        other sender on the connection has to rediscover it.
        """
        if self._closed or self._writer.peer_gone:
            return
        if self._writer.peer_is_gone():
            self._closed = self._writer.peer_gone = True
            if _DEBUG:
                logger.debug('sender: transport already closing; write skipped')
            return
        try:
            await write_fn(arg)
        except (ConnectionResetError, BrokenPipeError) as exc:
            self._closed = self._writer.peer_gone = True
            if _DEBUG:
                logger.debug('sender: peer closed write side (%s)', exc.__class__.__name__)
        except OSError as exc:
            # SSLEOFError / SSLZeroReturnError land here on TLS connections
            # whose peer dropped without a proper close-notify.
            self._closed = self._writer.peer_gone = True
            if _DEBUG:
                logger.debug('sender: write failed on closed TLS transport (%s)', exc.__class__.__name__)

    async def _write(self, data: bytes):
        """Flush *data* through the writer (peer-close tolerant)."""
        await self._guarded_write(self._writer.write, data)

    async def _write_many(self, parts) -> None:
        """Write *parts* (peer-close tolerant), choosing join vs vectored I/O.

        Senders express *what* they have — a response that naturally exists as
        separate fragments — and this method owns *how* it goes out.  It is the
        one decision point: docs/about/internals.md §Send-path invariant.
        """
        if sum(map(len, parts)) <= _VECTORED_JOIN_THRESHOLD:
            await self._guarded_write(self._writer.write, b''.join(parts))
        else:
            await self._guarded_write(self._writer.writelines, parts)


class HTTP1Sender(BaseSender):
    """Translates content or ASGI HTTP send events into HTTP/1.1 wire-format bytes.

    ``__call__`` accepts two forms:

    **High-level** (bytes body + status):
      ``await sender(body_bytes, HTTPStatus.OK, headers=[...])``
      Writes the status line, headers, blank line, and body in one call.

    **Low-level** (ASGI event dict, for internal/error-handler use):
      ``await sender({'type': 'http.response.start', ...})``
      ``await sender({'type': 'http.response.body', ...})``

    ``http.response.start`` is buffered until ``http.response.body`` arrives so
    that Content-Length can be injected when the app omits it.
    """

    __slots__ = (
        '_buffered_status', '_buffered_headers', '_chunked',
        '_expect_trailers', '_head_mode', '_log_record', '_started',
        '_completed', '_trailers_started', '_content_length',
        '_body_bytes', '_suppress_body', '_informational',
        '_response_started', '_poisoned',
    )

    def __init__(self, writer: AbstractWriter):
        super().__init__(writer)
        self._buffered_status: HTTPStatus | None = None
        self._buffered_headers: Headers | None = None
        self._chunked: bool = False
        self._expect_trailers: bool = False
        # Set True once the status line + headers have hit the wire
        # (any path through ``_flush`` / ``_pathsend``).  HTTP1Actor
        # consults this after BB_REQUEST_TIMEOUT expiry to decide
        # whether a synthetic 408 can still be emitted.
        self._started: bool = False
        # Set True once a complete response has been written for this request.
        # Further response events are then dropped, so a handler that raises
        # *after* completing its response cannot write a second one onto the
        # same keep-alive connection (the H2 post-END_STREAM drop).
        self._completed: bool = False
        self._trailers_started: bool = False
        self._content_length: int | None = None
        self._body_bytes: int = 0
        self._suppress_body: bool = False
        self._informational: bool = False
        # Tracks application response activity independently of ``_started``.
        # The latter means final headers reached the wire and is intentionally
        # false for buffered starts and informational responses.
        self._response_started: bool = False
        self._poisoned: bool = False
        # RFC 9110 §9.3.2 — when the request was HEAD, the response must
        # have the same headers (including Content-Length) as a GET would
        # but no body.  HTTP1Actor sets this before dispatch.
        self._head_mode: bool = False
        # Optional access-log record; set by the actor before dispatch.  The
        # sender's arms update it inline rather than through a per-event
        # capturing ``send`` wrapper, whose coroutine dispatch measured ~7% of
        # HTTP/1.1 CPU.  ``None`` means no capture.
        self._log_record = None

    async def __call__(self, body: _SenderBody,
                       status: HTTPStatus = HTTPStatus.OK,
                       headers: HeaderList = ()):
        """Dispatch on *body* and write the resulting HTTP/1.1 bytes.

        Accepted forms:

        - ``bytes`` — emit a complete response: status line, headers
          (with ``Content-Length`` injected if absent), blank line, body.
        - ``{'type': 'http.response.start', ...}`` — buffer the status,
          headers, and ``trailers`` flag; nothing is written yet.
        - ``{'type': 'http.response.body', ...}`` — on the first call after a
          buffered start, flush the start (adding ``Content-Length`` for
          single-body responses or ``Transfer-Encoding: chunked`` when
          ``more_body=True``); subsequent calls write chunk-framed body bytes
          and the terminal ``0\\r\\n\\r\\n`` when streaming completes without
          declared trailers.
        - ``{'type': 'http.response.trailers', ...}`` — write ``0\\r\\n`` once,
          followed by trailer fields; the final event adds the empty line.

        Unknown event types are logged and dropped; non-dict / non-bytes
        bodies raise ``TypeError``.
        """
        if self._completed or self._poisoned:
            return

        begins_response = (
            isinstance(body, bytes)
            or (isinstance(body, NativeResponse) and body._header is not None)
            or (isinstance(body, dict)
                and body.get('type') == ASGIEvent.HTTP_RESPONSE_START)
        )
        if self._started and begins_response:
            # Once final headers are on the wire, another response head would
            # splice a second status line into the unfinished message.
            self._poisoned = True
            return

        match body:
            case bytes():
                self._response_started = True
                h = headers if isinstance(headers, Headers) else Headers(headers)
                if self._log_record is not None:
                    self._log_record.status = int(status)
                    self._log_record.response_bytes += len(body)
                await self._flush(status, h, body)
                if not _is_informational(status):
                    self._completed = True

            case NativeResponse():
                # One object may carry header, body, and/or trailers; each arm
                # does what the correspondingly named dict arm below does.
                if body._header is not None:
                    self._response_started = True
                    self._buffered_status = HTTPStatus(body.status)
                    # Preserve the ASGI start `trailers: True` flag so a
                    # terminal body before the trailers event withholds the
                    # terminal chunk (lossless full-form compat).
                    self._expect_trailers = body.expects_trailers
                    header_pairs = list(body._header)
                    self._buffered_headers = Headers(header_pairs)
                    if self._log_record is not None:
                        self._log_record.status = body.status
                        self._log_record.mark('start_arm_in')
                        for hk, hv in body._header:
                            if isinstance(hk, bytes):
                                hkl = hk.lower()
                                if hkl == b'content-type':
                                    self._log_record.resp_content_type = hv
                                elif hkl == b'content-encoding':
                                    self._log_record.resp_content_encoding = hv
                        self._log_record.mark('start_arm_out')
                if body.file_path is not None:
                    if await self._pathsend(body.file_path):
                        self._completed = True
                    return
                if body.body is not None:
                    self._response_started = True
                    await self._handle_body_content(body._body, body.more_body)
                if body.trailers is not None and not self._completed:
                    self._response_started = True
                    await self._handle_trailers(
                        body.trailers, body.more_trailers)

            case {'type': ASGIEvent.HTTP_RESPONSE_START}:
                self._response_started = True
                self._buffered_status = HTTPStatus(body.get('status', HTTPStatus.OK))
                self._expect_trailers = bool(body.get('trailers', False))
                header_pairs = list(body.get('headers', []))
                self._buffered_headers = Headers(header_pairs)
                if self._log_record is not None:
                    self._log_record.status = body.get('status', '-')
                    self._log_record.mark('start_arm_in')
                    for hk, hv in body.get('headers', []):
                        if isinstance(hk, bytes):
                            hkl = hk.lower()
                            if hkl == b'content-type':
                                self._log_record.resp_content_type = hv
                            elif hkl == b'content-encoding':
                                self._log_record.resp_content_encoding = hv
                    self._log_record.mark('start_arm_out')

            case {'type': ASGIEvent.HTTP_RESPONSE_BODY}:
                self._response_started = True
                await self._handle_body_content(body.get('body', b''),
                                                body.get('more_body', False))

            case {'type': ASGIEvent.HTTP_RESPONSE_TRAILERS}:
                self._response_started = True
                await self._handle_trailers(
                    body.get('headers', []),
                    bool(body.get('more_trailers', False)))

            case {'type': ASGIEvent.HTTP_RESPONSE_PATHSEND}:
                self._response_started = True
                if await self._pathsend(body['path']):
                    self._completed = True

            case {'type': str() as event_type}:
                logger.warning('HTTP1Sender: unknown event type %r', event_type)

            case _:
                raise TypeError(f'HTTP1Sender expected bytes or dict, got {type(body)!r}')

    async def _handle_body_content(self, content: bytes, more_body: bool) -> None:
        """Write one body chunk — shared by the dict and native paths."""
        # These two marks bracket the last body event's transport write, so a
        # slow response splits into handler work before it and drain inside it.
        if self._log_record is not None and not more_body:
            self._log_record.mark('body_arm_in')
        if self._buffered_status is not None:
            assert self._buffered_headers is not None
            await self._flush(self._buffered_status, self._buffered_headers, content, more_body)
            self._buffered_status = None
            self._buffered_headers = None
        else:
            self._track_content_length(len(content), more_body)
            if self._chunked and not self._suppress_body:
                if content:
                    chunk = f'{len(content):x}\r\n'.encode() + content + b'\r\n'
                    if not more_body and not self._expect_trailers:
                        chunk += b'0\r\n\r\n'
                    await self._write(chunk)
                elif not more_body and not self._expect_trailers:
                    await self._write(b'0\r\n\r\n')
            elif content and not self._suppress_body:
                await self._write(content)
        if self._log_record is not None and content:
            self._log_record.response_bytes += len(content)
        if self._suppress_body:
            if self._log_record is not None and not more_body:
                self._log_record.mark('body_arm_out')
            if not more_body:
                if self._informational:
                    self._informational = False
                    self._suppress_body = False
                else:
                    self._completed = True
            return
        if self._log_record is not None and not more_body:
            self._log_record.mark('body_arm_out')
        if (not more_body
                and (not self._expect_trailers or self._head_mode)):
            self._completed = True

    async def _handle_trailers(self, headers: HeaderList,
                               more_trailers: bool = False) -> None:
        """Write one part of the trailer section for dict and native paths."""
        if not (self._expect_trailers or self._chunked):
            return
        if not self._trailers_started:
            await self._write(b'0\r\n')
            self._trailers_started = True
        for name, value in headers:
            await self._write(name + b': ' + value + b'\r\n')
        if not more_trailers:
            await self._write(b'\r\n')
            self._expect_trailers = False
            self._completed = True

    def reset_per_request_state(self) -> None:
        # One HTTP1Sender serves every request on a keep-alive connection, so
        # any slot left behind here becomes the next request's framing.
        self._buffered_status = None
        self._buffered_headers = None
        self._chunked = False
        self._expect_trailers = False
        self._started = False
        self._completed = False
        self._trailers_started = False
        self._content_length = None
        self._body_bytes = 0
        self._suppress_body = False
        self._informational = False
        self._response_started = False
        self._poisoned = False
        self._head_mode = False
        self._log_record = None

    def _ensure_framing_headers(self, status: HTTPStatus, headers: Headers,
                                body_len: int, more_body: bool) -> Headers:
        """Derive the sole legal framing from status and body mode.

        Transfer-Encoding belongs to the server because it describes bytes on
        the transport, not the application payload.  Content-Length is parsed
        before rebuilding the field list so duplicate values cannot create two
        competing message boundaries.
        """
        code = int(status)
        self._chunked = False
        self._content_length = None
        self._body_bytes = 0
        self._informational = _is_informational(status)
        content_forbidden = self._informational or code in (204, 205, 304)
        self._suppress_body = self._head_mode or content_forbidden

        keep_length = (not self._informational and code not in (204, 205)
                       and not (self._expect_trailers and not self._head_mode))
        app_length = _parse_content_length(headers) if keep_length else None
        pairs = [
            (name, value) for name, value in headers
            if name.lower() not in (b'content-length', b'transfer-encoding')
        ]

        if self._informational or code == 204:
            self._expect_trailers = False
        elif code == 205:
            self._expect_trailers = False
            pairs.append((b'content-length', b'0'))
        elif code == 304:
            self._expect_trailers = False
            if app_length is not None:
                pairs.append((b'content-length',
                              _content_length_bytes(app_length)))
        elif self._expect_trailers and not self._head_mode:
            pairs.append((b'transfer-encoding', b'chunked'))
            self._chunked = True
        elif more_body:
            if app_length is None:
                pairs.append((b'transfer-encoding', b'chunked'))
                self._chunked = True
            else:
                pairs.append((b'content-length',
                              _content_length_bytes(app_length)))
                self._content_length = app_length
        else:
            expected = body_len
            if app_length is not None and app_length != expected:
                raise ValueError(
                    'Content-Length does not match the response body')
            pairs.append((b'content-length',
                          _content_length_bytes(expected)))
            self._content_length = expected

        return Headers(pairs)

    def _track_content_length(self, content_len: int, more_body: bool) -> None:
        """Reject a declared-length stream that crosses its wire boundary."""
        if self._content_length is None:
            return
        total = self._body_bytes + content_len
        if total > self._content_length:
            if self._started:
                self._poisoned = True
            raise ValueError('response body exceeds Content-Length')
        if not more_body and total != self._content_length:
            if self._started:
                self._poisoned = True
            raise ValueError('response body is shorter than Content-Length')
        self._body_bytes = total

    @staticmethod
    def _ensure_date_header(headers: Headers) -> None:
        # RFC 9110 §6.6.1 — origin server SHOULD generate Date.  The check is
        # case-sensitive because the HTTP/1.1 path stores headers in the
        # framework's canonical capitalisation; HTTP/2 needs ``_has_header``.
        if b'Date' not in headers:
            headers.append(b'Date', _http_date())

    async def _flush(self, status: HTTPStatus, headers: Headers, body: bytes, more_body: bool = False) -> None:
        headers = self._ensure_framing_headers(
            status, headers, len(body), more_body)
        self._track_content_length(len(body), more_body)
        if not _is_informational(status):
            self._started = True
        self._ensure_date_header(headers)

        # Coalescing status line, headers and body into one write makes the
        # response one drain instead of one per header line: uncoalesced, a
        # 3-header response cost ~6 event-loop yields and measured ~33% of
        # HTTP/1.1 CPU.
        head = self._render_start(status, headers)

        # Headers still go out; only the body is suppressed (RFC 9110 §9.3.2
        # for HEAD, and the codes whose content is forbidden).
        if self._suppress_body:
            await self._write(head)
            return

        if self._chunked:
            if body:
                chunk = head + f'{len(body):x}\r\n'.encode() + body + b'\r\n'
            else:
                chunk = head
            if not more_body and not self._expect_trailers:
                chunk += b'0\r\n\r\n'
            await self._write(chunk)
        elif body:
            await self._write_many((head, body))
        else:
            await self._write(head)

    def _render_start(self, status: HTTPStatus, headers: HeaderList) -> bytes:
        """Build the status line + headers + blank-line as a single bytes blob."""
        parts: list[bytes] = [_status_line(status)]
        for k, v in headers:
            parts.append(k)
            parts.append(b': ')
            parts.append(v)
            parts.append(_CRLF)
        parts.append(_CRLF)
        return b''.join(parts)

    async def _pathsend(self, path: str) -> bool:
        """Handle ``http.response.pathsend`` — write headers, then sendfile.

        The file size supplies the known representation length.  A caller's
        Content-Length must agree with it before the headers are written.

        Falls back to a buffered read+write loop if the underlying
        transport does not support sendfile (TLS, mocked tests).
        HEAD requests get headers only.
        """
        if self._buffered_status is None or self._buffered_headers is None:
            logger.warning('HTTP1Sender: pathsend without buffered start; dropping')
            return False
        if self._expect_trailers and not self._head_mode:
            raise ValueError('pathsend cannot be combined with response trailers')

        size = os.path.getsize(path)
        headers = self._buffered_headers
        status = self._buffered_status
        headers = self._ensure_framing_headers(
            status, headers, size, more_body=False)
        self._track_content_length(size, more_body=False)
        if not _is_informational(status):
            self._started = True
        self._ensure_date_header(headers)

        head = self._render_start(status, headers)
        self._buffered_status = None
        self._buffered_headers = None

        if self._log_record is not None:
            self._log_record.response_bytes += size

        if self._suppress_body:
            await self._write(head)
            return not _is_informational(status)

        await self._write(head)

        try:
            with open(path, 'rb') as f:
                offset = 0
                try:
                    while offset < size:
                        sent = await self._writer.sendfile(
                            f, offset, size - offset)
                        if sent <= 0 or sent > size - offset:
                            raise ConnectionResetError(
                                'sendfile made no valid forward progress')
                        offset += sent
                    return not _is_informational(status)
                except NotImplementedError:
                    # TLS / unsupported transport — fall back to read+write.
                    f.seek(offset)
                    remaining = size - offset
                    while remaining > 0:
                        chunk = await asyncio.to_thread(
                            f.read, min(_PATHSEND_FALLBACK_CHUNK, remaining))
                        if not chunk:
                            raise ConnectionResetError(
                                'pathsend file ended before Content-Length')
                        remaining -= len(chunk)
                        await self._write(chunk)
        except BaseException:
            # The response head is already committed, so no error handler can
            # safely replace this response on the same connection.
            self._poisoned = True
            raise
        return not _is_informational(status)


class FlowControlStalled(Exception):
    """The peer never granted the flow-control credit it was asked for.

    Distinct from a write failure: the socket is fine and the peer is
    answering — it simply declines to accept the response it requested,
    which is the "data dribble" shape of CVE-2019-9511.  Carried as its
    own type so the stream ends with ``RST_STREAM(CANCEL)`` (a stream we
    gave up on) rather than ``INTERNAL_ERROR`` (a server that broke).
    """


class ConnectionWindow:
    """Shared HTTP/2 connection-level (stream 0) send flow-control window.

    One instance per connection, referenced by every stream's
    [`HTTP2Sender`][], so all senders debit and await a single budget.

    Without sharing each sender held a *private copy* of the
    connection window and debited only that copy, while the actor-level total
    was only ever incremented — so N concurrent streams could each spend a
    full 65535-byte window and the server could emit N×65535 bytes with zero
    real stream-0 credit.  A strict peer (nghttp2, grpc-go) treats that as a
    connection ``FLOW_CONTROL_ERROR`` and GOAWAYs (RFC 9113 §6.9.1).

    The object is a thin mutable holder: senders read/debit ``size`` directly
    and the owning actor fans out wake-ups to blocked senders on a
    connection-level ``WINDOW_UPDATE`` (it already tracks every live sender).
    """

    __slots__ = ('size',)

    def __init__(self, size: int = DEFAULT_INITIAL_WINDOW_SIZE) -> None:
        self.size = size


class HTTP2Sender(BaseSender):
    """Translates content or ASGI HTTP send events into HTTP/2 frames.

    ``__call__`` accepts four forms:

    **High-level** (bytes body + status):
      ``await sender(body_bytes, HTTPStatus.OK, headers=[...])``
      Sends a HEADERS frame followed by a DATA frame.

    **Native** ([`NativeResponse`][blackbull.native.NativeResponse]):
      ``await sender(NativeResponse(status=..., header=..., body=...))``
      One object may carry header, body, and/or trailers; the sender buffers
      the header arm exactly like the dict start and delegates body/trailers
      to the shared helpers (HEADERS + DATA [+ trailing HEADERS] coalesce).

    **Low-level** (ASGI event dict):
      ``await sender({'type': 'http.response.start', ...})``
      ``await sender({'type': 'http.response.body', ...})``

    **Control-plane** (raw FrameBase instance):
      ``await sender(settings_frame)``
      Serialises and writes the frame directly.
    """

    __slots__ = (
        '_factory', '_stream_id', '_push_callback',
        '_conn_window', 'stream_window_size',
        'max_frame_size', '_window_open', '_end_stream_sent',
        '_flow_control_timeout',
        '_flow_control_cap',
        '_buffered_status', '_buffered_headers', '_expect_trailers',
        '_buffered_body', '_buffered_trailers', '_auto_flush_task',
        '_log_record',
    )

    def __init__(self, writer: AbstractWriter, factory, stream_id: int,
                 push_callback=None,
                 conn_window: 'ConnectionWindow | None' = None,
                 initial_window: int | None = None,
                 flow_control_timeout: float | None = None,
                 flow_control_cap: str = 'write_timeout'):
        super().__init__(writer)
        self._factory = factory
        self._stream_id = stream_id
        self._push_callback = push_callback
        # A sender built without one gets a private window, which is correct
        # only for a lone stream ([`ConnectionWindow`][]).
        self._conn_window = conn_window if conn_window is not None else ConnectionWindow()
        # A plain int, not a per-stream mapping: one sender serves one stream,
        # and keying it would invite a reader to hunt for multi-stream
        # semantics that do not exist.  Callers pass ``initial_window`` so a
        # sender created after the SETTINGS exchange starts at the peer's
        # announced window rather than the RFC 9113 §6.9.2 default.
        self.stream_window_size = (DEFAULT_INITIAL_WINDOW_SIZE
                                   if initial_window is None else initial_window)
        self.max_frame_size = DEFAULT_MAX_FRAME_SIZE
        self._window_open: asyncio.Event | None = None
        # How long the peer may take to grant flow-control credit before the
        # stream gives up.  Every caller that builds senders in bulk passes it,
        # because one sender is created *per stream* and the fallback below
        # puts a function-level import — resolved through
        # ``importlib._bootstrap`` on every execution — on the per-request
        # path.  The fallback serves direct instantiation only.
        if flow_control_timeout is None:
            from ..env import get_settings  # noqa: PLC0415
            flow_control_timeout = get_settings().write_timeout
        self._flow_control_timeout: float = flow_control_timeout
        self._flow_control_cap = flow_control_cap
        self._end_stream_sent: bool = False
        self._buffered_status: HTTPStatus | None = None
        self._buffered_headers: list[tuple[bytes, bytes]] | None = None
        self._expect_trailers: bool = False
        # Holds the first single-frame body chunk when trailers are expected,
        # so HEADERS + DATA + trailing HEADERS coalesce into one write.
        self._buffered_body: bytes | None = None
        # HTTP/2 has one terminal trailer field section.  ASGI may deliver
        # that section over multiple events, so hold non-terminal parts until
        # one HEADERS block can carry END_STREAM.
        self._buffered_trailers: list[tuple[bytes, bytes]] | None = None
        self._auto_flush_task: asyncio.Future | None = None
        # Optional access-log record, set by the actor.  Captured inline in the
        # arms below rather than through a capturing ``send`` wrapper, which is
        # dict-shaped and so would never match the native seam.
        self._log_record = None

    @property
    def connection_window_size(self) -> int:
        """The shared connection-level send window.

        A property over [`ConnectionWindow`][], not a per-sender field:
        every sender on the connection reads and writes the same value.
        """
        return self._conn_window.size

    @connection_window_size.setter
    def connection_window_size(self, value: int) -> None:
        self._conn_window.size = value

    def reset_per_request_state(self) -> None:
        self._end_stream_sent = False
        self._buffered_status = None
        self._buffered_headers = None
        self._expect_trailers = False
        self._buffered_body = None
        self._buffered_trailers = None
        self._log_record = None
        self._auto_flush_task = None

    async def _write_response_start_and_body(
        self, body: bytes, end_stream: bool,
        status: HTTPStatus, headers: list[tuple[bytes, bytes]] | None,
        expect_trailers: bool,
    ) -> None:
        """Write the deferred response HEADERS + first DATA body chunk together.

        Called on every first body event (buffered or not), not only for the
        auto-flush of a held chunk — which is what the name says and a
        "flush the buffered start" name would not.
        """
        headers = headers or []
        # END_STREAM rides the DATA frame below, never HEADERS.
        h_bytes = build_response_headers(
            self._factory.encoder, self._stream_id, status, headers,
            end_stream=False)

        total = len(body)
        sid_bytes = self._stream_id.to_bytes(4, 'big')
        set_end_stream = end_stream and not expect_trailers

        if (total <= self._conn_window.size and
                total <= self.stream_window_size and
                total <= self.max_frame_size):
            end_flag = DataFrameFlags.END_STREAM if set_end_stream else 0
            if total == 0:
                d_bytes = b'\x00\x00\x00\x00' + end_flag.to_bytes(1, 'big') + sid_bytes
            else:
                d_bytes = (total.to_bytes(3, 'big') + b'\x00'
                           + end_flag.to_bytes(1, 'big') + sid_bytes + body)
            await self._write_flow_controlled((h_bytes, d_bytes), total)
        else:
            await super()._write(h_bytes)
            await self._write_data(body, end_stream=set_end_stream)
        if set_end_stream:
            self._end_stream_sent = True

    def _schedule_auto_flush(self) -> None:
        """Schedule a deferred flush of the just-buffered first body chunk.

        A single ``ensure_future`` hop, not a ``call_soon`` →
        ``ensure_future`` two-hop: the task's first step runs at the next
        event-loop iteration, *after* any synchronous ASGI events emitted in the
        same coroutine burst — so trailers (or a second body chunk) still get a
        chance to consume the buffer and coalesce before the task fires.  The
        buffered tuple is snapshotted here and passed to the task, decoupling the
        flush from whatever the live ``_buffered_*`` slots hold when it runs
        (``reset_per_request_state`` sender reuse)."""
        task = asyncio.ensure_future(self._do_auto_flush(
            self._buffered_body, self._buffered_status,
            self._buffered_headers, self._expect_trailers))
        self._auto_flush_task = task
        task.add_done_callback(self._on_auto_flush_done)

    def _on_auto_flush_done(self, task: asyncio.Future) -> None:
        """Clear the slot and surface any non-connection failure.  Connection
        errors are already swallowed inside ``_guarded_write``, so anything that
        reaches here (e.g. an HPACK encode error) is a real bug worth logging."""
        if task is self._auto_flush_task:
            self._auto_flush_task = None
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            logger.error(
                'HTTP2Sender auto-flush failed on stream %d: %r',
                self._stream_id, exc, exc_info=exc)

    async def _do_auto_flush(
        self, body: bytes | None, status: HTTPStatus | None,
        headers: list[tuple[bytes, bytes]] | None, expect: bool,
    ) -> None:
        """Flush the snapshotted buffered body + headers when the producer has
        parked (no synchronous trailers or second body arrived in the same
        event-loop iteration).

        Fires only if the *exact* chunk this task was scheduled for is still the
        pending one (identity guard): a synchronous trailers / second-body event
        — or a ``reset_per_request_state`` reuse of this sender — replaces or
        clears ``_buffered_body`` first, in which case this is a no-op."""
        if self._buffered_body is not body or body is None or status is None:
            return
        self._buffered_body = None
        self._buffered_status = None
        self._buffered_headers = None
        await self._write_response_start_and_body(body, False, status, headers, expect)

    async def send_response_headers(
        self, status: HTTPStatus, headers: list[tuple[bytes, bytes]],
    ) -> None:
        """Write a standalone HEADERS frame (END_HEADERS, no END_STREAM) now.

        Unlike the ``http.response.start`` event — which is buffered until a
        body event so HEADERS + first DATA can coalesce into one write — this
        flushes the response HEADERS immediately and leaves the stream open.
        Required by the RFC 8441 WebSocket-over-HTTP/2 accept: the
        ``:status 200`` response carries no body, so nothing would ever trigger
        the deferred flush, and the stream must stay open bidirectionally for
        the subsequent WebSocket DATA frames.
        """
        await self._write(build_response_headers(
            self._factory.encoder, self._stream_id, status, headers,
            end_stream=False))

    async def _write(self, data: bytes):
        """Write a frame to the transport.

        Per RFC 7540 §6.9.1, only DATA frames are subject to flow control;
        HEADERS and control frames (SETTINGS, PING, WINDOW_UPDATE, RST_STREAM,
        GOAWAY, CONTINUATION) are not.  Flow-controlled writes go through
        [`_write_data`][].
        """
        await super()._write(data)

    async def _write_data(self, body: bytes, end_stream: bool) -> None:
        """Send *body* as one or more DATA frames, respecting flow control and max frame size.

        Splits the body into chunks of at most
        ``min(connection_window_size, stream_window_size, max_frame_size)`` bytes
        (RFC 7540 §6.9 and §4.2), waiting for WINDOW_UPDATE between chunks when
        flow-control credit is exhausted.  END_STREAM is set only on the last frame.
        """
        total = len(body)

        sid_bytes = self._stream_id.to_bytes(4, 'big')

        if total == 0:
            flags = DataFrameFlags.END_STREAM if end_stream else 0
            await super()._write(b'\x00\x00\x00\x00' + flags.to_bytes(1, 'big') + sid_bytes)
            return

        offset = 0
        while offset < total:
            if self._closed or self._writer.peer_gone:
                return
            while (self._conn_window.size <= 0 or
                   self.stream_window_size <= 0):
                if self._window_open is None:
                    self._window_open = asyncio.Event()
                self._window_open.clear()
                # Re-check after clear(): a WINDOW_UPDATE delivered by the
                # frame loop between the loop condition above and this
                # clear() would have ``set()`` the event, and the clear()
                # would then discard that wake-up.  Without this guard we
                # would ``await`` an event no further WINDOW_UPDATE will set
                # → permanent block (lost-wakeup race, RFC 9113 §6.9).
                if (self._conn_window.size > 0 and
                        self.stream_window_size > 0):
                    break
                # A peer that requests a large response and never opens its
                # window parks this task forever (CVE-2019-9511's shape).
                # The caller supplies the owner for this progress wait:
                # servers use ``BB_WRITE_TIMEOUT`` and clients inject their
                # own ``BB_CLIENT_WRITE_TIMEOUT`` without changing this
                # shared sender.
                if self._flow_control_timeout <= 0:
                    await self._window_open.wait()
                    continue
                try:
                    async with asyncio.timeout(self._flow_control_timeout):
                        await self._window_open.wait()
                except (asyncio.TimeoutError, TimeoutError) as exc:
                    if self._flow_control_cap == 'write_timeout':
                        log_cap_hit('write_timeout',
                                    requested=self._flow_control_timeout,
                                    limit=self._flow_control_timeout,
                                    protocol='http2')
                    else:
                        log_cap_hit(self._flow_control_cap,
                                    requested=self._flow_control_timeout,
                                    limit=self._flow_control_timeout,
                                    protocol='http2')
                    cap_env = f'BB_{self._flow_control_cap.upper()}'
                    raise FlowControlStalled(
                        f'stream {self._stream_id}: peer sent no WINDOW_UPDATE '
                        f'within {cap_env}={self._flow_control_timeout}s'
                    ) from exc

            chunk_size = min(
                self._conn_window.size,
                self.stream_window_size,
                self.max_frame_size,
                total - offset,
            )

            is_last = (offset + chunk_size >= total)
            flags = DataFrameFlags.END_STREAM if (is_last and end_stream) else 0
            chunk = body[offset:offset + chunk_size]
            frame_header = (chunk_size.to_bytes(3, 'big') + b'\x00'
                            + flags.to_bytes(1, 'big') + sid_bytes)
            await self._write_flow_controlled((frame_header, chunk), chunk_size)
            offset += chunk_size

    async def _write_flow_controlled(
        self, parts: tuple[bytes, ...], payload_size: int,
    ) -> None:
        """Commit DATA credit before a writer can suspend in write/drain.

        Callers select a payload fitting both windows and enter this method
        without an intervening await. Debit both windows in that same event
        loop turn so parallel streams cannot spend an in-flight write's
        credit. The frame header and any coalesced HEADERS consume no credit.

        Never refund on cancellation or failure: a writer may have delivered
        bytes before raising. Peer WINDOW_UPDATE replenishes credit, and
        INITIAL_WINDOW_SIZE deltas adjust stream credit; transport/stream
        teardown owns stopping failed producers.
        """
        self._conn_window.size -= payload_size
        self.stream_window_size -= payload_size
        await self._write_many(parts)

    def window_update(self, increment: int) -> None:
        self.stream_window_size += increment
        self.wake_window()

    def wake_window(self) -> None:
        """Wake any blocked _write_data() after a window credit change."""
        if self._window_open is not None:
            self._window_open.set()

    def apply_settings(self, max_frame_size: int | None = None) -> None:
        """Apply SETTINGS parameters that do not require delta tracking."""
        if max_frame_size is not None:
            self.max_frame_size = max_frame_size

    def adjust_initial_window(self, delta: int) -> None:
        """RFC 9113 §6.9.2 — adjust this sender's stream flow-control window
        by the change in SETTINGS_INITIAL_WINDOW_SIZE since the peer's last
        announcement.  The window may legitimately become negative.
        """
        self.stream_window_size += delta
        if delta > 0:
            self.wake_window()

    async def _handle_body_content(self, payload: bytes, end_stream: bool) -> None:
        """Write one body chunk — shared by the dict and native H2 paths.

        ``payload`` is the chunk; ``end_stream`` is True for the **terminal**
        chunk (note the polarity flip vs ``HTTP1Sender._handle_body_content``,
        which takes ``more_body`` — the negation).  A terminal chunk must not
        carry END_STREAM while trailers are pending: END_STREAM belongs on
        the trailing HEADERS (RFC 9113 §8.1 — frames after END_STREAM are a
        protocol error).
        """
        if self._log_record is not None and payload:
            self._log_record.response_bytes += len(payload)
        if self._log_record is not None and end_stream:
            self._log_record.mark('body_arm_in')
        if self._buffered_status is not None:
            # Trailers-coalescing fast path: when trailers are expected
            # and this is the first single-frame body chunk, hold it so
            # HEADERS + DATA + trailing HEADERS flush together at the
            # trailers event (halves the writes+drains for a unary RPC).
            # Only for a non-terminal chunk that fits one DATA frame and
            # the current flow-control windows.
            if (self._expect_trailers and not end_stream
                    and self._buffered_body is None
                    and 0 < len(payload) <= self.max_frame_size
                    and len(payload) <= self.stream_window_size
                    and len(payload) <= self._conn_window.size):
                self._buffered_body = payload
                self._schedule_auto_flush()
            else:
                if self._buffered_body is not None:
                    buffered_body = self._buffered_body
                    buffered_status = self._buffered_status
                    buffered_headers = self._buffered_headers
                    # Take ownership before drain can yield to auto-flush.
                    self._buffered_status = None
                    self._buffered_headers = None
                    self._buffered_body = None
                    await self._write_response_start_and_body(
                        buffered_body, False, buffered_status,
                        buffered_headers, self._expect_trailers)
                    await self._write_data(
                        payload,
                        end_stream=end_stream and not self._expect_trailers)
                else:
                    await self._write_response_start_and_body(
                        payload, end_stream, self._buffered_status,
                        self._buffered_headers, self._expect_trailers)
                    self._buffered_status = None
                    self._buffered_headers = None
        else:
            await self._write_data(
                payload, end_stream=end_stream and not self._expect_trailers)
        if self._log_record is not None and end_stream:
            self._log_record.mark('body_arm_out')
        if end_stream and not self._expect_trailers:
            self._end_stream_sent = True

    async def _handle_trailers(
        self,
        headers: list[tuple[bytes, bytes]],
        more_trailers: bool = False,
    ) -> None:
        """Write the trailing HEADERS — shared by the dict and native H2 paths.

        Takes a plain ``list`` of pairs (the H2 variant; ``HTTP1Sender``'s
        same-named helper takes a ``HeaderList``).

        HPACK's dynamic table is stateful, so header blocks MUST be encoded in
        wire order: the response HEADERS block first, then the trailing HEADERS
        block.  Encoding trailers before the deferred HEADERS would desync the
        peer's HPACK decoder.
        """
        if more_trailers:
            if self._buffered_trailers is None:
                self._buffered_trailers = headers
            else:
                self._buffered_trailers.extend(headers)
            return
        if self._buffered_trailers is not None:
            self._buffered_trailers.extend(headers)
            headers = self._buffered_trailers
            self._buffered_trailers = None

        if self._buffered_status is not None:
            buffered_body = self._buffered_body
            self._buffered_body = None
            h_bytes = build_response_headers(
                self._factory.encoder, self._stream_id,
                self._buffered_status, self._buffered_headers or [],
                end_stream=False)
            if buffered_body is not None:
                # Buffering reserves no credit: another stream can spend it
                # before these trailers arrive. Recheck at handoff so the
                # combined DATA cannot exceed either peer window.
                if (len(buffered_body) <= self._conn_window.size
                        and len(buffered_body) <= self.stream_window_size
                        and len(buffered_body) <= self.max_frame_size):
                    total = len(buffered_body)
                    trailer_bytes = build_trailers(
                        self._factory.encoder, self._stream_id, headers)
                    d_bytes = (total.to_bytes(3, 'big') + b'\x00'
                               + b'\x00'  # DATA flags: no END_STREAM (trailers carry it)
                               + self._stream_id.to_bytes(4, 'big')
                               + buffered_body)
                    await self._write_flow_controlled(
                        (h_bytes, d_bytes, trailer_bytes), total)
                else:
                    await self._write(h_bytes)
                    await self._write_data(buffered_body, end_stream=False)
                    # Encode only after the credit wait: another stream may
                    # send HEADERS during it, and the HPACK table is shared.
                    await self._write(build_trailers(
                        self._factory.encoder, self._stream_id, headers))
            else:
                trailer_bytes = build_trailers(
                    self._factory.encoder, self._stream_id, headers)
                await self._write_many((h_bytes, trailer_bytes))
            self._buffered_status = None
            self._buffered_headers = None
        else:
            await self._write(build_trailers(
                self._factory.encoder, self._stream_id, headers))
        self._expect_trailers = False
        self._end_stream_sent = True

    async def __call__(self, body: _SenderBody | FrameBase,
                       status: HTTPStatus = HTTPStatus.OK,
                       headers: HeaderList = []):
        if isinstance(body, FrameBase):
            if _DEBUG:
                logger.debug('HTTP2Sender raw frame: %r', body)
            await self._write(body.save())
            return

        if isinstance(body, bytes):
            # RFC 9113 §8.1, as in the dict branch below.
            if self._end_stream_sent:
                logger.warning(
                    'HTTP2Sender: dropping bytes write on stream %d — '
                    'END_STREAM already sent (ASGI app sent a body after '
                    'the response was complete)',
                    self._stream_id)
                return
            if self._log_record is not None:
                self._log_record.status = int(status)
                self._log_record.response_bytes += len(body)
            h_bytes = build_response_headers(
                self._factory.encoder, self._stream_id, status, headers,
                end_stream=False)

            total = len(body)
            sid_bytes = self._stream_id.to_bytes(4, 'big')
            if (total <= self._conn_window.size and
                    total <= self.stream_window_size and
                    total <= self.max_frame_size):
                end_flag = DataFrameFlags.END_STREAM.to_bytes(1, 'big')
                if total == 0:
                    d_bytes = b'\x00\x00\x00\x00' + end_flag + sid_bytes
                else:
                    d_bytes = total.to_bytes(3, 'big') + b'\x00' + end_flag + sid_bytes + body
                await self._write_flow_controlled((h_bytes, d_bytes), total)
            else:
                await super()._write(h_bytes)
                await self._write_data(body, end_stream=True)
            self._end_stream_sent = True

        elif isinstance(body, NativeResponse):
            if self._end_stream_sent:
                logger.warning(
                    'HTTP2Sender: dropping NativeResponse on stream %d — '
                    'END_STREAM already sent (ASGI app sent a response after '
                    'the response was complete)',
                    self._stream_id)
                return
            if body._header is not None:
                self._buffered_status = HTTPStatus(body.status)
                self._buffered_headers = list(body._header)
                self._expect_trailers = body.expects_trailers
                if self._log_record is not None:
                    self._log_record.status = body.status
                    self._log_record.mark('start_arm_in')
                    for hk, hv in body._header:
                        if isinstance(hk, bytes):
                            hkl = hk.lower()
                            if hkl == b'content-type':
                                self._log_record.resp_content_type = hv
                            elif hkl == b'content-encoding':
                                self._log_record.resp_content_encoding = hv
                    self._log_record.mark('start_arm_out')
            if body.body is not None:
                await self._handle_body_content(body._body, not body.more_body)
            if body.trailers is not None and not self._end_stream_sent:
                await self._handle_trailers(
                    list(body.trailers), body.more_trailers)

        elif isinstance(body, dict):
            event_type = body.get('type', '')
            if _DEBUG:
                logger.debug('HTTP2Sender event: %r', event_type)

            # RFC 9113 §8.1 — frames after END_STREAM are a protocol error.
            # Drop the event with a warning rather than writing a frame that
            # the peer would treat as a stream error.  Application bug to
            # surface; sender's job is to not make it worse on the wire.
            if self._end_stream_sent:
                logger.warning(
                    'HTTP2Sender: dropping %r on stream %d — END_STREAM already '
                    'sent (ASGI app sent an event after the response was complete)',
                    event_type, self._stream_id)
                return

            if event_type == ASGIEvent.HTTP_RESPONSE_START:
                # Buffered, not written: HEADERS coalesces with the first body.
                self._buffered_status = HTTPStatus(body.get('status', 200))
                self._buffered_headers = list(body.get('headers', []))
                self._expect_trailers = bool(body.get('trailers', False))
                if self._log_record is not None:
                    self._log_record.status = body.get('status', '-')
                    self._log_record.mark('start_arm_in')
                    for hk, hv in body.get('headers', []):
                        if isinstance(hk, bytes):
                            hkl = hk.lower()
                            if hkl == b'content-type':
                                self._log_record.resp_content_type = hv
                            elif hkl == b'content-encoding':
                                self._log_record.resp_content_encoding = hv
                    self._log_record.mark('start_arm_out')

            elif event_type == ASGIEvent.HTTP_RESPONSE_BODY:
                await self._handle_body_content(
                    body.get('body', b''), not body.get('more_body', False))

            elif event_type == ASGIEvent.HTTP_RESPONSE_TRAILERS:
                await self._handle_trailers(
                    list(body.get('headers', [])),
                    bool(body.get('more_trailers', False)))

            elif event_type == ASGIEvent.HTTP_RESPONSE_PUSH:
                if self._push_callback is not None:
                    await self._push_callback(body, self._stream_id)
                else:
                    logger.warning('http.response.push received but no push handler registered')

            else:
                logger.info('HTTP2Sender: unhandled event type %r', event_type)

        else:
            raise TypeError(f'HTTP2Sender expected bytes, dict, or FrameBase, got {type(body)!r}')


class WebSocketSender(BaseSender):
    """Translates ASGI websocket send events or WebSocketResponse dicts into
    WebSocket wire frames (RFC 6455).

    ``__call__`` accepts an ASGI event dict (as returned by ``WebSocketResponse``):
      - ``{'type': 'websocket.send', 'text': ...}``  → text frame (opcode 0x1)
      - ``{'type': 'websocket.send', 'bytes': ...}`` → binary frame (opcode 0x2)
      - ``{'type': 'websocket.close'}``              → close frame (opcode 0x8)
      - ``{'type': 'websocket.accept'}``             → no-op (handshake already sent)

    The ``status`` and ``headers`` parameters are accepted for interface
    consistency but are unused for WebSocket connections.
    """

    __slots__ = ('_compressor',)

    def __init__(self, writer: AbstractWriter, *, compressor=None):
        super().__init__(writer)
        # An [`OutboundCompressor`][] when permessage-deflate is negotiated;
        # ``None`` sends outbound frames verbatim (RSV1=0).
        self._compressor = compressor

    def _frame_payload(self, raw: bytes,
                       opcode: WSOpcode) -> tuple[bytes, bytes]:
        """Frame one data payload into ``(header, payload)``.

        Shared by the native and dict arms so the two cannot put different
        bytes on the wire.  **Sync on purpose**: nothing here suspends —
        compressing and building a header are pure computation — and when this
        was an ``async def`` every send allocated and awaited a coroutine that
        never yielded, for 67 ns on a ~700 ns send.  The caller awaits
        [`_write_many`][], which is the only part that can block.

        The pair is written vectored, so the payload is never copied into a
        concatenated frame buffer (the join ``encode_frame`` would allocate).
        """
        rsv1 = self._compressor is not None
        if rsv1:
            raw = self._compressor.compress(raw)
        return encode_frame_header(len(raw), opcode, rsv1=rsv1), raw

    async def _send_close(self, code: int) -> None:
        await self._write(encode_frame(code.to_bytes(2, 'big'),
                                       opcode=WSOpcode.CLOSE))

    async def __call__(self, body: _WSSenderEvent | NativeWSMessage,
                       _status: HTTPStatus | None = None,
                       _headers: HeaderList = []):
        # Dict arm first.  A dict is the one shape here that nothing cheaper
        # than ``isinstance`` can recognise, and it is what the external-host
        # edge and the raw (conn, receive, send) compat form emit.  Testing it
        # first means the compat path pays one check, and the native arm
        # below pays that same one on its way past — no second type guard
        # on either lane.
        if isinstance(body, dict):
            event_type = body.get('type', '')

            match event_type:

                case ASGIEvent.WS_SEND:
                    if 'text' in body and body['text'] is not None:
                        await self._write_many(self._frame_payload(
                            body['text'].encode('utf-8'), WSOpcode.TEXT))
                    else:
                        await self._write_many(self._frame_payload(
                            body.get('bytes', b''), WSOpcode.BINARY))

                case ASGIEvent.WS_CLOSE:
                    await self._send_close(body.get('code', WSCloseCode.NORMAL))

                case ASGIEvent.WS_ACCEPT:
                    pass  # handshake reply sent by HTTP1Actor._do_ws_handshake()
                case _:
                    logger.warning('WebSocketSender: unknown event type %r',
                                   event_type)
            return

        if isinstance(body, NativeWSMessage):
            match body.kind:
                case NativeWSMessage.SEND:
                    if body.text is not None:
                        await self._write_many(self._frame_payload(
                            body.text.encode('utf-8'), WSOpcode.TEXT))
                    else:
                        await self._write_many(self._frame_payload(
                            body.data or b'', WSOpcode.BINARY))
                case NativeWSMessage.CLOSE:
                    await self._send_close(body.code
                                           if body.code is not None
                                           else WSCloseCode.NORMAL)
                case NativeWSMessage.ACCEPT:
                    pass
                case _:
                    logger.warning('WebSocketSender: unknown native kind %r',
                                   body.kind)
            return

        raise TypeError(
            f'WebSocketSender expected a NativeWSMessage or a dict, '
            f'got {type(body)!r}')


class SenderFactory:
    """Creates the appropriate BaseSender for the given protocol.

    All methods accept a raw asyncio-compatible stream writer and wrap it in
    ``AsyncioWriter`` internally.  To support a different async runtime,
    implement a new ``AbstractWriter`` subclass and pass it directly to the
    sender constructors instead.
    """

    @staticmethod
    def _ensure_writer(stream_writer) -> AbstractWriter:
        """Normalise a raw asyncio stream writer to an ``AbstractWriter``.

        Passes an ``AbstractWriter`` through unchanged (a caller-supplied
        runtime adapter); otherwise wraps the raw writer in ``AsyncioWriter``.
        """
        if isinstance(stream_writer, AbstractWriter):
            return stream_writer
        return AsyncioWriter(stream_writer)

    @staticmethod
    def http1(stream_writer) -> HTTP1Sender:
        return HTTP1Sender(SenderFactory._ensure_writer(stream_writer))

    @staticmethod
    def http2(stream_writer, factory, stream_id: int,
              push_callback=None,
              conn_window: 'ConnectionWindow | None' = None,
              initial_window: int | None = None,
              flow_control_timeout: float | None = None) -> HTTP2Sender:
        return HTTP2Sender(SenderFactory._ensure_writer(stream_writer),
                           factory, stream_id, push_callback,
                           conn_window=conn_window,
                           initial_window=initial_window,
                           flow_control_timeout=flow_control_timeout)

    @staticmethod
    def websocket(stream_writer, *, compressor=None) -> WebSocketSender:
        return WebSocketSender(SenderFactory._ensure_writer(stream_writer),
                               compressor=compressor)
