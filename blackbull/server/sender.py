"""Protocol send boundaries.

Always route fragments through BaseSender._write_many; protocol senders do
not choose join versus vectored writes. Writer adapters owe both paths.
"""
import asyncio
import os
import time
from abc import ABC, abstractmethod
from http import HTTPStatus
from functools import cache
from inspect import iscoroutinefunction
from email.utils import formatdate
from itertools import chain
from typing import NoReturn

from ..protocol import hpack_fastpath
from ..env import get_settings
from ..protocol.framing import (NO_CONTENT_GENERATED_STATUSES, is_informational,
                                parse_content_length)
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
from ..headers import (
    HeaderList, _MinimalResponseHeaders, _as_response_fields)
from ..native import NativeResponse, NativeWSMessage, _native_from_asgi

from ..logger import debug_gate  # noqa: E402
logger = logging.getLogger(__name__)
_DEBUG = debug_gate(logger)


_CRLF = b'\r\n'

# Fallback chunk size when ``sendfile`` isn't supported by the transport
# (TLS, mocked tests).  Matches the static middleware's ``_CHUNK`` so
# memory-peak guarantees stay consistent across paths.
_PATHSEND_FALLBACK_CHUNK = 64 * 1024

_SENDFILE_CHUNK = 1024 * 1024

# All protocol fragments use this internal join/vectored size gate.
_VECTORED_JOIN_THRESHOLD = 32 * 1024


_STATUS_BY_CODE: dict[int, HTTPStatus] = {s.value: s for s in HTTPStatus}

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


# Date has whole-second resolution; cache only within the same second.
_HTTP_DATE_TS: int = 0
_HTTP_DATE: bytes = b''


def _http_date() -> bytes:
    global _HTTP_DATE_TS, _HTTP_DATE
    now = int(time.time())
    if now != _HTTP_DATE_TS:
        _HTTP_DATE = formatdate(timeval=now, localtime=False, usegmt=True).encode('ascii')
        _HTTP_DATE_TS = now
    return _HTTP_DATE


# The two builders below must stay byte-for-byte equivalent to the frame-object
# path they replace — ``protocol.frame_types.Headers.save()`` — including how
# the shared HPACK dynamic table
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
    object path does.  *headers* is read, not mutated.
    """
    headers = _as_response_fields(headers)
    fields = headers if headers.date else (*headers, (b'date', _http_date()))

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
    headers = _as_response_fields(headers)
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

    # Peer death is shared by every sender on this connection writer.
    peer_gone: bool = False

    @abstractmethod
    async def write(self, data: bytes) -> None:
        """Write *data* to the transport and ensure it is flushed."""

    async def writelines(self, parts) -> None:
        """Write ordered byte parts; the default joins before writing.

        Transports may override this to avoid joining. Copy behavior is transport-specific.
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


@cache
def _reports_writing_paused(kind: type) -> bool:
    return isinstance(getattr(kind, 'writing_paused', None), property)


@cache
def _lingers(kind: type) -> bool:
    """Whether *kind* defines ``linger_close`` as a coroutine function."""
    return iscoroutinefunction(getattr(kind, 'linger_close', None))


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
        # Same mock hazard: checked on the class, which a fabricated instance
        # attribute cannot reach.
        self._linger = (stream_writer.linger_close
                        if _lingers(type(stream_writer)) else None)
        self._skips_unpaused_drain = _reports_writing_paused(type(stream_writer))

    async def _drain_with_timeout(self) -> None:
        """Drain the underlying StreamWriter, bounded by ``_write_timeout``.

        On timeout, close the transport (so the FD/connection slot is
        reclaimed from a slow-read peer or dead TCP route) and surface a
        ``ConnectionResetError`` so the sender's existing peer-disconnect
        handling runs uniformly.  When no timeout is configured this is a
        plain ``drain()``.
        """
        dl = self._deadline
        if dl is None or (self._skips_unpaused_drain
                          and not self._sw.writing_paused):
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
        """Delegate ordered byte parts to StreamWriter; its transport chooses how to write.
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
        # Writes have drained; close without waiting for transport shutdown.
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


# Byte-send conveniences are sender-specific; keep ASGI callable types unchanged.
_SenderEvent = ASGISendEvent
_SenderBody = _SenderEvent | bytes | NativeResponse
_WSSenderEvent = WebSocketSendEvent | WebSocketCloseEvent | WebSocketAcceptEvent


class BaseSender(ABC):
    """Protocol senders accepting native messages, compatible ASGI events, or bytes.

    Byte writes go through AbstractWriter; send parts through _write_many so its
    shared size gate selects joining versus vectored transport writes.
    """

    __slots__ = ('_writer', '_closed')

    def __init__(self, writer: AbstractWriter):
        self._writer = writer
        self._closed = False

    async def _settle_buffered_head(self) -> None:
        """A head that never got content is complete — settle it before the
        next one would overwrite it."""
        if self._buffered_status is not None:
            await self._send_interim(self._buffered_status,
                                    self._buffered_headers)

    def mark_client_gone(self) -> None:
        """Drop further writes when the actor detects a dead peer.

        http.disconnect remains a receive-side event, never a send argument.
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
        'supports_interim',
        '_buffered_status', '_buffered_headers', '_chunked',
        '_expect_trailers', '_head_mode', '_log_record', '_started',
        '_completed', '_trailers_started', '_content_length',
        '_body_bytes', '_suppress_body', '_informational',
        '_response_started', '_poisoned',
    )

    def __init__(self, writer: AbstractWriter, *,
                 supports_interim: bool = True):
        super().__init__(writer)
        self.supports_interim = supports_interim
        self._buffered_status: HTTPStatus | None = None
        self._buffered_headers: _MinimalResponseHeaders | None = None
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
        # None disables per-request access capture.
        self._log_record = None

    async def __call__(self, body: _SenderBody,
                       status: HTTPStatus = HTTPStatus.OK,
                       headers: HeaderList = ()):
        """Dispatch supported native or ASGI response events and bytes.

        Buffer a start until body framing is known. Streaming without Content-Length
        uses chunked encoding; declared trailers own the terminator. Unknown ASGI
        event types are logged and dropped; unsupported values raise TypeError.
        """
        if self._completed or self._poisoned:
            return

        if isinstance(body, dict):
            body = _native_from_asgi(body, copy_headers=False)

        if (isinstance(body, NativeResponse) and body._extension is not None
                and body.push is not None):
            logger.warning('HTTP1Sender: push sent on HTTP/1; dropped')
            return

        begins_response = (isinstance(body, bytes)
                           or (isinstance(body, NativeResponse)
                               and body._header is not None))
        if self._started and begins_response:
            # Once final headers are on the wire, another response head would
            # splice a second status line into the unfinished message.
            self._poisoned = True
            return

        match body:
            case bytes():
                self._response_started = True
                h = _as_response_fields(headers)
                if self._log_record is not None:
                    self._log_record.status = int(status)
                    self._log_record.response_bytes += len(body)
                await self._flush(status, h, body)
                if not is_informational(status):
                    self._completed = True

            case NativeResponse():
                if body._header is not None:
                    head = _as_response_fields(body._header)
                    self._response_started = True
                    await self._settle_buffered_head()
                    self._buffered_status = (_STATUS_BY_CODE.get(body.status)
                                             or HTTPStatus(body.status))
                    # Preserve the ASGI start `trailers: True` flag so a
                    # terminal body before the trailers event withholds the
                    # terminal chunk (lossless full-form compat).
                    self._expect_trailers = body.expects_trailers
                    self._buffered_headers = head
                    if self._log_record is not None:
                        self._log_record.status = body.status
                        self._log_record.mark('start_arm_in')
                        for hk, hv in head:
                            if hk == b'content-type':
                                self._log_record.resp_content_type = hv
                            elif hk == b'content-encoding':
                                self._log_record.resp_content_encoding = hv
                        self._log_record.mark('start_arm_out')
                if body._extension is not None:
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
        headers = _as_response_fields(headers)
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

    def _ensure_framing_headers(self, status: HTTPStatus,
                                head: _MinimalResponseHeaders,
                                body_len: int, more_body: bool) -> list:
        """Derive the sole legal framing from status and body mode.

        Transfer-Encoding belongs to the server because it describes bytes on
        the transport, not the application payload.  Content-Length is parsed
        before rebuilding the field list so duplicate values cannot create two
        competing message boundaries.

        Returns a new field list; *head* is not modified.
        """
        code = int(status)
        self._chunked = False
        self._content_length = None
        self._body_bytes = 0
        # One classification answers both questions below. A 304 is bodyless
        # but keeps Content-Length as metadata, where a 205 does not.
        informational = is_informational(status)
        contentless = informational or code in (204, 205)
        self._informational = informational
        self._suppress_body = (self._head_mode or contentless
                               or code == 304)
        keep_length = (not contentless
                       and not (self._expect_trailers and not self._head_mode))
        lengths = head.content_length
        if not (keep_length and lengths):
            app_length = None
        elif len(lengths) == 1 and lengths[0][1].isdigit():
            app_length = int(lengths[0][1])
        else:
            app_length = parse_content_length(lengths)
        if head.transfer_encoding:
            pairs = [
                (name, value) for name, value in head
                if name not in (b'content-length', b'transfer-encoding')
            ]
        elif lengths:
            pairs = list(head)
            for field in lengths:
                pairs.remove(field)
        else:
            pairs = list(head)

        if informational or code == 204:
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

        return pairs

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
    def _ensure_date_header(fields: list, head: _MinimalResponseHeaders) -> None:
        # RFC 9110 §6.6.1 — origin server SHOULD generate Date.
        if not head.date:
            fields.append((b'date', _http_date()))

    async def _flush(self, status: HTTPStatus, head: _MinimalResponseHeaders,
                     body: bytes, more_body: bool = False) -> None:
        headers = self._ensure_framing_headers(
            status, head, len(body), more_body)
        self._track_content_length(len(body), more_body)
        if not self._informational:
            self._started = True
        self._ensure_date_header(headers, head)

        # Coalesce the response head and body before the shared write gate.
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

    async def _send_interim(self, status: HTTPStatus,
                            headers: HeaderList) -> None:
        """Settle a head that never got content, and leave the response
        open for its final one.  RFC 9110 §15.2 puts an interim response
        before the final one on the same response, so leaving it buffered
        would let the next head overwrite it.  A `start` settles the one
        before it, and the flush that already classifies the status does the
        work — nothing is asked twice.

        An HTTP/1.0 peer is sent nothing: §15.2 gives a 1xx to clients that
        speak HTTP/1.1, and the state is settled the same way because the
        interim still has no content and the final head still follows."""
        if self.supports_interim:
            await self._write(self._render_start(
                status,
                self._ensure_framing_headers(
                    status, headers, 0, more_body=False)))
        self._buffered_status = None
        self._buffered_headers = None
        self._expect_trailers = False
        # RFC 9112 §6.3 rule 1: an interim response carries no content, so a
        # body event cannot end it. The next `start` recomputes this.
        self._suppress_body = True

    def _render_start(self, status: HTTPStatus, headers: HeaderList) -> bytes:
        """Build the status line + headers + blank-line as a single bytes blob.

        *headers* must already be validated: the arms that buffer a head
        validate it, and the framing fields added here are the server's own.
        """
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
        head = self._buffered_headers
        status = self._buffered_status
        headers = self._ensure_framing_headers(
            status, head, size, more_body=False)
        self._track_content_length(size, more_body=False)
        if not self._informational:
            self._started = True
        self._ensure_date_header(headers, head)

        head = self._render_start(status, headers)
        self._buffered_status = None
        self._buffered_headers = None

        if self._log_record is not None:
            self._log_record.response_bytes += size

        if self._suppress_body:
            await self._write(head)
            return not is_informational(status)

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
                    return not is_informational(status)
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
        return not is_informational(status)


class FlowControlStalled(Exception):
    """The peer never granted the flow-control credit it was asked for.

    Distinct from a write failure: the socket is fine and the peer is
    answering — it simply declines to accept the response it requested,
    which is the "data dribble" shape of CVE-2019-9511.  Carried as its
    own type so the stream ends with ``RST_STREAM(CANCEL)`` (a stream we
    gave up on) rather than ``INTERNAL_ERROR`` (a server that broke).
    """


class ConnectionWindow:
    """Connection send credit shared by every stream (RFC 9113 §6.9.1).

    Debit before awaiting writes. Window changes may make stream credit negative;
    the actor wakes blocked senders on connection WINDOW_UPDATE.
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
        '_head_mode', '_suppress_body',
        '_log_record',
    )

    def __init__(self, writer: AbstractWriter, factory, stream_id: int,
                 push_callback=None,
                 conn_window: 'ConnectionWindow | None' = None,
                 initial_window: int | None = None,
                 flow_control_timeout: float | None = None,
                 flow_control_cap: str = 'write_timeout',
                 head_mode: bool = False):
        super().__init__(writer)
        self._head_mode = head_mode
        self._suppress_body = False
        self._factory = factory
        self._stream_id = stream_id
        self._push_callback = push_callback
        # A sender built without one gets a private window, which is correct
        # only for a lone stream ([`ConnectionWindow`][]).
        self._conn_window = conn_window if conn_window is not None else ConnectionWindow()
        # Use the peer SETTINGS window for senders created after negotiation.
        self.stream_window_size = (DEFAULT_INITIAL_WINDOW_SIZE
                                   if initial_window is None else initial_window)
        self.max_frame_size = DEFAULT_MAX_FRAME_SIZE
        self._window_open: asyncio.Event | None = None
        # How long the peer may take to grant flow-control credit before the
        # stream gives up.
        if flow_control_timeout is None:
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
        self._suppress_body = False
        self._log_record = None
        self._auto_flush_task = None

    def retire_stream(self) -> None:
        """Stop this stream's pending and future output without closing its peer.

        HTTP/2 senders share one transport, so stream retirement cannot use the
        writer's connection-wide ``peer_gone`` flag.  The per-sender closed bit
        drops later writes, while cancellation and the window wake release work
        already parked inside this sender.
        """
        self._closed = True
        task = self._auto_flush_task
        self._auto_flush_task = None
        if task is not None and not task.done():
            task.cancel()
        self._buffered_status = None
        self._buffered_headers = None
        self._buffered_body = None
        self._buffered_trailers = None
        self.wake_window()

    async def _write_response_start_and_body(
        self, body: bytes, end_stream: bool,
        status: HTTPStatus, headers: list[tuple[bytes, bytes]] | None,
        expect_trailers: bool,
    ) -> None:
        """Write response headers and the first body chunk together.
        """
        if self._closed:
            return
        headers = headers or []
        # END_STREAM rides the DATA frame below, never HEADERS — unless the
        # head was promised no content, when there is no DATA to ride on.
        informational = is_informational(status)
        forbidden = (self._head_mode or informational
                     or int(status) in (204, 205, 304))
        self._suppress_body = forbidden
        h_bytes = build_response_headers(
            self._factory.encoder, self._stream_id, status, headers,
            end_stream=forbidden and not informational)
        if forbidden:
            await self._write(h_bytes)
            if not is_informational(status):
                self._end_stream_sent = True
            return

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
        """Defer the first buffered chunk until the synchronous send burst finishes.

        Snapshot the buffered tuple: sender reuse may reset its live slots before
        the flush runs. Trailers or another chunk may consume it first.
        """
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
        """Flush only if the snapshotted chunk is still pending.

        A second chunk, trailers, or sender reset may consume/replace it first;
        then the identity guard makes this task a no-op.
        """
        if self._buffered_body is not body or body is None or status is None:
            return
        self._buffered_body = None
        self._buffered_status = None
        self._buffered_headers = None
        await self._write_response_start_and_body(body, False, status, headers, expect)

    async def _send_interim(self, status: HTTPStatus,
                            headers: list[tuple[bytes, bytes]],
                            ) -> None:
        """Write an interim head now and leave the stream open for its final
        one.  Both event arms funnel here.  RFC 9112 §6.1 forbids
        Content-Length and Transfer-Encoding in a contentless message, and
        transfer-encoding is connection-specific: RFC 9113 §8.2.2 keeps it
        out of HTTP/2 entirely."""
        kept = [(hk, hv) for hk, hv in headers
                if hk not in (b'content-length', b'transfer-encoding')]
        await self.send_response_headers(status, kept)
        self._buffered_status = None
        self._buffered_headers = None
        self._expect_trailers = False
        # RFC 9112 §6.3 rule 1: an interim carries no content, so a body
        # event cannot end it.
        self._suppress_body = True

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
        # Encoding mutates the connection-wide HPACK table.  A retired stream
        # cannot advance it because the peer will never receive this block.
        if self._closed:
            return
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
                if self._closed:
                    return
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
            # Hold the first nonterminal body chunk with headers for expected trailers.
            # Only coalesce if it fits one DATA frame and current flow-control windows.
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
                    if self._suppress_body:
                        # No content before the final head: RFC 9113 §8.1
                        # leaves an informational response part of the same
                        # exchange.
                        return
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
            if self._suppress_body:
                # No content before the final head: RFC 9113 §8.1 leaves an
                # informational response part of the same exchange.
                return
            await self._write_data(
                payload, end_stream=end_stream and not self._expect_trailers)
        if self._log_record is not None and end_stream:
            self._log_record.mark('body_arm_out')
        if end_stream and not self._expect_trailers and not self._suppress_body:
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
        if self._closed:
            return
        headers = _as_response_fields(headers)
        if more_trailers:
            if self._buffered_trailers is None:
                self._buffered_trailers = headers.copy()
            else:
                self._buffered_trailers.extend(headers)
            return
        if self._buffered_trailers is not None:
            self._buffered_trailers.extend(headers)
            headers = self._buffered_trailers
            self._buffered_trailers = None

        if self._suppress_body and self._buffered_status is None:
            # RFC 9112 §6.3 rule 1: no trailer section either. The head
            # already carried END_STREAM, so there is nothing left to say.
            self._expect_trailers = False
            return

        if self._buffered_status is not None:
            self._suppress_body = (
                self._head_mode
                or self._buffered_status in NO_CONTENT_GENERATED_STATUSES)
            buffered_body = self._buffered_body
            self._buffered_body = None
            if self._suppress_body:
                # RFC 9112 §6.3 rule 1: such a response "cannot contain a
                # message body or trailer section". The head terminates it.
                head_status = self._buffered_status
                informational = is_informational(head_status)
                await self._write(build_response_headers(
                    self._factory.encoder, self._stream_id,
                    head_status, self._buffered_headers or [],
                    end_stream=not informational))
                self._buffered_status = None
                self._buffered_headers = None
                if not informational:
                    self._end_stream_sent = True
                return
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
                    if not self._closed:
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
        if self._closed:
            return
        # Control-plane: raw frame object (SETTINGS, PING ACK, WINDOW_UPDATE, …)
        if isinstance(body, FrameBase):
            if _DEBUG:
                logger.debug('HTTP2Sender raw frame: %r', body)
            await self._write(body.save())
            return

        if isinstance(body, dict):
            body = _native_from_asgi(body, copy_headers=False)

        if isinstance(body, bytes):
            # RFC 9113 §8.1, as in the dict branch below.
            if self._end_stream_sent:
                if not self._suppress_body:
                    logger.warning(
                        'HTTP2Sender: dropping bytes write on stream %d — '
                        'END_STREAM already sent (ASGI app sent a body after '
                        'the response was complete)',
                        self._stream_id)
                return
            if self._log_record is not None:
                self._log_record.status = int(status)
                self._log_record.response_bytes += len(body)
            informational = is_informational(status)
            forbidden = (self._head_mode or informational
                         or int(status) in (204, 205, 304))
            self._suppress_body = forbidden
            h_bytes = build_response_headers(
                self._factory.encoder, self._stream_id, status, headers,
                end_stream=forbidden and not informational)
            if forbidden:
                await self._write(h_bytes)
                if not informational:
                    self._end_stream_sent = True
                return

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
                if not self._suppress_body:
                    logger.warning(
                        'HTTP2Sender: dropping NativeResponse on stream %d — '
                        'END_STREAM already sent (ASGI app sent a response after '
                        'the response was complete)',
                        self._stream_id)
                return
            if body._extension is not None and body.push is not None:
                if self._push_callback is not None:
                    await self._push_callback(body, self._stream_id)
                else:
                    logger.warning('push sent but no push handler registered')
                return
            if body._header is not None:
                head = _as_response_fields(body._header)
                await self._settle_buffered_head()
                self._buffered_status = HTTPStatus(body.status)
                self._buffered_headers = head
                self._expect_trailers = body.expects_trailers
                if self._log_record is not None:
                    self._log_record.status = body.status
                    self._log_record.mark('start_arm_in')
                    for hk, hv in head:
                        if hk == b'content-type':
                            self._log_record.resp_content_type = hv
                        elif hk == b'content-encoding':
                            self._log_record.resp_content_encoding = hv
                    self._log_record.mark('start_arm_out')
            if body.body is not None:
                await self._handle_body_content(body._body, not body.more_body)
            if body.trailers is not None and not self._end_stream_sent:
                await self._handle_trailers(
                    body.trailers, body.more_trailers)

        elif isinstance(body, dict):
            event_type = body.get('type', '')
            if _DEBUG:
                logger.debug('HTTP2Sender event: %r', event_type)

            # RFC 9113 §8.1 — frames after END_STREAM are a protocol error.
            # Drop the event with a warning rather than writing a frame that
            # the peer would treat as a stream error.  Application bug to
            # surface; sender's job is to not make it worse on the wire.
            if self._end_stream_sent:
                if not self._suppress_body:
                    logger.warning(
                        'HTTP2Sender: dropping %r on stream %d — END_STREAM already '
                        'sent (ASGI app sent an event after the response was complete)',
                        event_type, self._stream_id)
                return

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
        """Frame a data payload into separate header and payload parts.

        Native and compatibility sends share this synchronous framing. The caller
        awaits _write_many, which chooses joining or vectored writes by size.
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
    """Create protocol senders from an AbstractWriter or wrap an asyncio-compatible writer.
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
    def http1(stream_writer, *, supports_interim: bool = True) -> HTTP1Sender:
        return HTTP1Sender(SenderFactory._ensure_writer(stream_writer),
                           supports_interim=supports_interim)

    @staticmethod
    def http2(stream_writer, factory, stream_id: int,
              push_callback=None,
              conn_window: 'ConnectionWindow | None' = None,
              initial_window: int | None = None,
              flow_control_timeout: float | None = None,
              head_mode: bool = False) -> HTTP2Sender:
        return HTTP2Sender(SenderFactory._ensure_writer(stream_writer),
                           factory, stream_id, push_callback,
                           conn_window=conn_window,
                           initial_window=initial_window,
                           flow_control_timeout=flow_control_timeout,
                           head_mode=head_mode)

    @staticmethod
    def websocket(stream_writer, *, compressor=None) -> WebSocketSender:
        return WebSocketSender(SenderFactory._ensure_writer(stream_writer),
                               compressor=compressor)
