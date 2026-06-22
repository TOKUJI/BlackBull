import asyncio
import os
import time
from abc import ABC, abstractmethod
from http import HTTPStatus
from email.utils import formatdate

from ..protocol.frame_types import (FrameTypes, HeaderFrameFlags, DataFrameFlags,
                                    SettingFrameFlags, FrameBase, PseudoHeaders,
                                    DEFAULT_INITIAL_WINDOW_SIZE, DEFAULT_MAX_FRAME_SIZE)
from .cap_log import log_cap_hit
from .constants import WSCloseCode
from .ws_codec import WSOpcode, WSFrameBits, WSFrameHeader, encode_frame
import logging
from ..asgi import ASGIEvent
from ..headers import Headers, HeaderList

logger = logging.getLogger(__name__)

_CRLF = b'\r\n'

# Fallback chunk size when ``sendfile`` isn't supported by the transport
# (TLS, mocked tests).  Matches the static middleware's ``_CHUNK`` so
# memory-peak guarantees stay consistent across paths.
_PATHSEND_FALLBACK_CHUNK = 64 * 1024


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


# ---------------------------------------------------------------------------
# Writer abstraction — swap asyncio for trio/curio by implementing this ABC
# ---------------------------------------------------------------------------

class AbstractWriter(ABC):
    """Protocol-agnostic async byte-sink.

    ``write()`` is the single responsibility: deliver bytes and ensure they
    are flushed.  Backpressure, buffering, and draining are implementation
    details of each concrete subclass — callers never call ``drain()`` directly.

    Implementors wrap a concrete transport (asyncio.StreamWriter, trio
    MemorySendStream, curio socket, …).  ``BaseSender`` only depends on this
    interface, so switching the async runtime requires only a new subclass here.
    """

    @abstractmethod
    async def write(self, data: bytes) -> None:
        """Write *data* to the transport and ensure it is flushed."""
        ...

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

    ``write_timeout`` (seconds, ``0`` = disabled) bounds the time spent
    in ``drain()`` waiting for the kernel send buffer to flush.  Defends
    against the slow-read shape of slowloris: a client that reads the
    response 1 byte/sec fills the send buffer and our drain blocks
    indefinitely waiting for the peer's TCP window to reopen.  On
    timeout we close the transport and raise ``ConnectionResetError``
    so the sender treats the failure the same as a peer-side reset.
    """

    def __init__(self, stream_writer, write_timeout: float = 0.0):
        if not (hasattr(stream_writer, 'write') and hasattr(stream_writer, 'drain')):
            raise TypeError(
                f"AsyncioWriter requires an object with write() and drain(), "
                f"got {type(stream_writer)}"
            )
        self._sw = stream_writer
        self._write_timeout = write_timeout

    async def write(self, data: bytes) -> None:
        self._sw.write(data)
        if self._write_timeout > 0:
            try:
                await asyncio.wait_for(self._sw.drain(),
                                       timeout=self._write_timeout)
            except (asyncio.TimeoutError, TimeoutError):
                # Slow-read peer / dead TCP route — close the transport
                # so the FD is released and the connection slot is
                # reclaimed, then surface as a peer disconnect for the
                # sender's existing error-handling path.
                logger.warning(
                    'write timeout (%.1fs) exceeded — closing connection',
                    self._write_timeout)
                log_cap_hit('write_timeout',
                            requested=self._write_timeout,
                            limit=self._write_timeout)
                try:
                    self._sw.close()
                except Exception as close_exc:
                    # Best-effort transport teardown.  We're already in
                    # the timeout error path and the transport may be in
                    # a half-broken state (SSL aborted, FD already
                    # reaped by a sibling task, etc.); swallowing here
                    # lets us still raise ConnectionResetError below so
                    # the sender's existing peer-disconnect handling
                    # runs uniformly.
                    logger.debug(
                        'write timeout: transport.close() also failed (%s) — '
                        'continuing with ConnectionResetError', close_exc)
                raise ConnectionResetError(
                    f'write timeout after {self._write_timeout:.1f}s'
                ) from None
        else:
            await self._sw.drain()

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
        if self._write_timeout > 0:
            try:
                await asyncio.wait_for(self._sw.drain(),
                                       timeout=self._write_timeout)
            except (asyncio.TimeoutError, TimeoutError):
                logger.warning(
                    'write timeout (%.1fs) exceeded — closing connection',
                    self._write_timeout)
                log_cap_hit('write_timeout',
                            requested=self._write_timeout,
                            limit=self._write_timeout)
                try:
                    self._sw.close()
                except Exception as close_exc:
                    logger.debug(
                        'write timeout: transport.close() also failed (%s) — '
                        'continuing with ConnectionResetError', close_exc)
                raise ConnectionResetError(
                    f'write timeout after {self._write_timeout:.1f}s'
                ) from None
        else:
            await self._sw.drain()

    async def close(self) -> None:
        # ``self._sw.close()`` is synchronous: it initiates the TCP
        # shutdown and schedules the transport's ``connection_lost``
        # callback for a later loop iteration.  We DO NOT await
        # ``wait_closed()`` here — under burst-keepalive workloads
        # (HttpArena ``static`` at c=4096) awaiting it serializes the
        # connection-actor coroutine with the transport-close completion,
        # adding 1-3 event-loop turns per connection.  With thousands of
        # simultaneous closes that latency multiplies into a multi-second
        # drain that monopolises the loop and starves the next wrk run.
        #
        # Safety: every ``write()`` above flushes via ``drain()``, so by
        # the time we reach close() there is no buffered payload.  The
        # transport tears down asynchronously; our coroutine exiting
        # earlier is harmless for the connection-actor path (no
        # follow-up state to flush).
        self._sw.close()

    async def sendfile(self, file, offset: int, count: int) -> int:
        """Zero-copy ``loop.sendfile`` against the underlying transport.

        Raises ``NotImplementedError`` (propagated from the loop) when
        the transport is SSL — TLS framing happens in user-space, so
        the kernel can't see the plaintext to copy.  Callers must catch
        that and fall back to a read+write loop.

        Drains any pending writes first so headers we already buffered
        precede the file bytes in wire order.
        """
        await self._sw.drain()
        loop = asyncio.get_running_loop()
        return await loop.sendfile(self._sw.transport, file, offset, count)


# ---------------------------------------------------------------------------
# Sender hierarchy
# ---------------------------------------------------------------------------

class BaseSender(ABC):
    """Abstract base for ASGI-event → wire-format senders.

    ``__call__`` accepts either:
      - ``bytes`` body + optional ``status`` and ``headers``: the sender builds
        and sends the full protocol response (start + body) in one call.
      - A protocol-specific event dict: dispatched to the appropriate handler.

    The actual byte transport is hidden behind ``AbstractWriter`` so the sender
    logic is decoupled from asyncio internals.
    """

    # Per-stream / per-request senders are allocated on the hot path; ABCs
    # provide ``__slots__ = ()`` so adding slots here drops the per-instance
    # ``__dict__``.  Subclasses extend with their own protocol-specific
    # slots; together they declare every attribute referenced by the class.
    __slots__ = ('_writer', '_closed')

    def __init__(self, writer: AbstractWriter):
        self._writer = writer
        # ``_write`` / ``_write_many`` flip this to True on a peer-closed
        # transport and silently drop further writes.  Initialised here so
        # that the slot is bound from construction; the ``getattr(...,
        # False)`` reads in the write methods remain compatible.
        self._closed = False

    @abstractmethod
    async def __call__(self, body, status: HTTPStatus = HTTPStatus.OK, headers: HeaderList = []): ...

    async def _write(self, data: bytes):
        """Flush *data* through the writer.

        Tolerant of peer-closed transports: once a write hits
        ``ConnectionResetError`` / ``BrokenPipeError`` / SSL EOF, the
        sender marks itself closed and subsequent writes silently drop.
        These exceptions used to propagate out as tracebacks under
        wrk c=1024 sustained load — 22 per 30 s in the 141848 run.
        """
        if getattr(self, '_closed', False):
            return
        try:
            await self._writer.write(data)
        except (ConnectionResetError, BrokenPipeError) as exc:
            self._closed = True
            logger.debug('sender: peer closed write side (%s)', exc.__class__.__name__)
        except OSError as exc:
            # SSLEOFError / SSLZeroReturnError land here on TLS connections
            # whose peer dropped without a proper close-notify.
            self._closed = True
            logger.debug('sender: write failed on closed TLS transport (%s)', exc.__class__.__name__)

    async def _write_many(self, parts) -> None:
        """Vectored variant of :meth:`_write` — avoids joining the parts.

        Used by the HTTP/1.1 ``_flush`` for the headers+body cache-hit
        path: the body bytes already live in the static-file cache, and
        ``head + body`` would allocate a full-body copy on every hit.
        """
        if getattr(self, '_closed', False):
            return
        try:
            await self._writer.writelines(parts)
        except (ConnectionResetError, BrokenPipeError) as exc:
            self._closed = True
            logger.debug('sender: peer closed write side (%s)', exc.__class__.__name__)
        except OSError as exc:
            self._closed = True
            logger.debug('sender: write failed on closed TLS transport (%s)', exc.__class__.__name__)


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
        # RFC 9110 §9.3.2 — when the request was HEAD, the response must
        # have the same headers (including Content-Length) as a GET would
        # but no body.  HTTP1Actor sets this before dispatch.
        self._head_mode: bool = False
        # Optional access-log record; set by the actor before dispatch.
        # When non-None, ``__call__`` updates ``status`` and
        # ``response_bytes`` inline as events flow through — this saves
        # the per-event coroutine dispatch through ``_make_capturing_send``
        # (~7% of HTTP/1.1 CPU in the profile).  When None, no capture.
        self._log_record = None

    async def __call__(self, body,
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
          and the terminal ``0\\r\\n\\r\\n`` when streaming completes.
        - ``{'type': 'http.response.trailers', ...}`` — write the terminal
          ``0\\r\\n`` followed by the trailer headers (chunked encoding).

        Unknown event types are logged and dropped; non-dict / non-bytes
        bodies raise ``TypeError``.
        """
        match body:
            case bytes():
                h = headers if isinstance(headers, Headers) else Headers(headers)
                if self._log_record is not None:
                    self._log_record.status = int(status)
                    self._log_record.response_bytes += len(body)
                await self._flush(status, h, body)

            case {'type': ASGIEvent.HTTP_RESPONSE_START}:
                self._buffered_status = HTTPStatus(body.get('status', HTTPStatus.OK))
                self._buffered_headers = Headers(list(body.get('headers', [])))
                self._expect_trailers = bool(body.get('trailers', False))
                if self._log_record is not None:
                    self._log_record.status = body.get('status', '-')
                    # Sprint 35 phase-trace: capture response headers
                    # inline (same pattern as response_bytes capture) so
                    # we can correlate per-phase µs against negotiated
                    # Content-Type / Content-Encoding without re-walking
                    # the headers list elsewhere.  No-op when PHASE_TRACE
                    # is off because the AccessLogRecord fields are
                    # already empty defaults.
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
                content = body.get('body', b'')
                more_body = body.get('more_body', False)
                if self._log_record is not None and content:
                    self._log_record.response_bytes += len(content)
                # Sprint 35 phase trace: bracket the actual transport
                # write for the last body event so we can see whether the
                # 30-60 ms woff2 tail lives in middleware/handler work
                # before the write (``start_arm_out → body_arm_in``) or
                # inside the write + drain (``body_arm_in → body_arm_out``).
                if self._log_record is not None and not more_body:
                    self._log_record.mark('body_arm_in')
                if self._buffered_status is not None:
                    assert self._buffered_headers is not None
                    await self._flush(self._buffered_status, self._buffered_headers, content, more_body)
                    self._buffered_status = None
                    self._buffered_headers = None
                else:
                    if self._head_mode:
                        # already wrote headers; HEAD response carries no body
                        if self._log_record is not None and not more_body:
                            self._log_record.mark('body_arm_out')
                        return
                    if self._chunked:
                        if content:
                            chunk = f'{len(content):x}\r\n'.encode() + content + b'\r\n'
                            if not more_body and not self._expect_trailers:
                                chunk += b'0\r\n\r\n'
                            await self._write(chunk)
                        elif not more_body and not self._expect_trailers:
                            await self._write(b'0\r\n\r\n')
                    elif content:
                        await self._write(content)
                if self._log_record is not None and not more_body:
                    self._log_record.mark('body_arm_out')

            case {'type': ASGIEvent.HTTP_RESPONSE_TRAILERS}:
                await self._write(b'0\r\n')
                for name, value in body.get('headers', []):
                    await self._write(name + b': ' + value + b'\r\n')
                await self._write(b'\r\n')

            case {'type': ASGIEvent.HTTP_RESPONSE_PATHSEND}:
                await self._pathsend(body['path'])

            case {'type': ASGIEvent.HTTP_DISCONNECT}:
                # http1_actor.py sends this on IncompleteReadError; the
                # canonical channel for disconnect is receive(), but
                # accommodating the actor's signal here makes future
                # writes no-ops so the broken pipe doesn't surface as
                # a traceback in _write.
                self._closed = True

            case {'type': str() as event_type}:
                logger.warning('HTTP1Sender: unknown event type %r', event_type)

            case _:
                raise TypeError(f'HTTP1Sender expected bytes or dict, got {type(body)!r}')

    def reset_per_request_state(self) -> None:
        # Sprint 38 lesson — HTTP1Sender is shared across keep-alive requests;
        # forgetting a reset silently breaks the next request's framing
        # (`_started` skipped 408 emission on the second request).  See
        # `.claude/patterns/cautions.md` — Sprint 38 section.
        self._buffered_status = None
        self._buffered_headers = None
        self._chunked = False
        self._expect_trailers = False
        self._started = False
        self._head_mode = False
        self._log_record = None

    def _ensure_framing_headers(self, headers: Headers, body_len: int, more_body: bool) -> None:
        if more_body:
            if b'transfer-encoding' not in headers:
                headers.append(b'transfer-encoding', b'chunked')
            self._chunked = True
        elif b'content-length' not in headers:
            headers.append(b'content-length', str(body_len).encode())

    @staticmethod
    def _ensure_date_header(headers: Headers) -> None:
        # RFC 9110 §6.6.1 — origin server SHOULD generate Date.  The
        # check is case-sensitive against b'Date' because the HTTP/1.1
        # path stores headers in the framework's canonical capitalisation;
        # the HTTP/2 path uses a separate _has_header() lookup because
        # RFC 9113 §8.2.1 mandates lowercase.
        if b'Date' not in headers:
            headers.append(b'Date', _http_date())

    async def _flush(self, status: HTTPStatus, headers: Headers, body: bytes, more_body: bool = False) -> None:
        self._started = True
        self._ensure_framing_headers(headers, len(body), more_body)
        self._ensure_date_header(headers)

        # Coalesce status line + headers + body into a single write so the
        # response is emitted as one TLS record / one drain.  Before this,
        # each header line was a separate `_write` (= a separate
        # `await drain()` yield); a 3-header response did ~6 yields per
        # request and showed in py-spy as ~33% of HTTP/1.1 CPU spread
        # across `_write_start` / `_write` / `streams.write`.
        head = self._render_start(status, headers)

        # RFC 9110 §9.3.2 — HEAD response carries no body.  Headers (and
        # the Content-Length we just computed from the GET body) still go
        # out so caches and proxies remain accurate.
        if self._head_mode:
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
            # Vectored write: avoids the full-body memcpy that ``head + body``
            # forced.  At static-file rates of ~5k req/s × ~17 KB on average,
            # that allocation was ~88 MB/s of pure user-space copy before the
            # bytes even reached the transport.
            await self._write_many((head, body))
        else:
            await self._write(head)

    def _render_start(self, status: HTTPStatus, headers: HeaderList) -> bytes:
        """Build the status line + headers + blank-line as a single bytes blob."""
        parts: list[bytes] = [f'HTTP/1.1 {status} {status.phrase}'.encode(), _CRLF]
        for k, v in headers:
            parts.append(k)
            parts.append(b': ')
            parts.append(v)
            parts.append(_CRLF)
        parts.append(_CRLF)
        return b''.join(parts)

    async def _pathsend(self, path: str) -> None:
        """Handle ``http.response.pathsend`` — write headers, then sendfile.

        Per the ASGI ``http.response.pathsend`` extension the caller
        already sent ``http.response.start`` with Content-Length set
        from the file size; we just need to flush those headers (no
        body bytes) and stream the file via ``writer.sendfile``.

        Falls back to a chunked read+write loop if the underlying
        transport does not support sendfile (TLS, mocked tests).
        HEAD requests get headers only.
        """
        if self._buffered_status is None or self._buffered_headers is None:
            logger.warning('HTTP1Sender: pathsend without buffered start; dropping')
            return

        self._started = True
        size = os.path.getsize(path)
        headers = self._buffered_headers
        self._ensure_framing_headers(headers, size, more_body=False)
        self._ensure_date_header(headers)

        head = self._render_start(self._buffered_status, headers)
        self._buffered_status = None
        self._buffered_headers = None

        if self._log_record is not None:
            self._log_record.response_bytes += size

        if self._head_mode:
            await self._write(head)
            return

        await self._write(head)

        with open(path, 'rb') as f:
            try:
                await self._writer.sendfile(f, 0, size)
                return
            except NotImplementedError:
                # TLS / unsupported transport — fall back to read+write.
                f.seek(0)
                remaining = size
                while remaining > 0:
                    chunk = await asyncio.to_thread(
                        f.read, min(_PATHSEND_FALLBACK_CHUNK, remaining))
                    if not chunk:
                        break
                    remaining -= len(chunk)
                    await self._write(chunk)


class HTTP2Sender(BaseSender):
    """Translates content or ASGI HTTP send events into HTTP/2 frames.

    ``__call__`` accepts three forms:

    **High-level** (bytes body + status):
      ``await sender(body_bytes, HTTPStatus.OK, headers=[...])``
      Sends a HEADERS frame followed by a DATA frame.

    **Low-level** (ASGI event dict):
      ``await sender({'type': 'http.response.start', ...})``
      ``await sender({'type': 'http.response.body', ...})``

    **Control-plane** (raw FrameBase instance):
      ``await sender(settings_frame)``
      Serialises and writes the frame directly.
    """

    __slots__ = (
        '_factory', '_stream_id', '_push_callback',
        'connection_window_size', 'stream_window_size',
        'max_frame_size', '_window_open', '_end_stream_sent',
        '_buffered_status', '_buffered_headers', '_expect_trailers',
    )

    def __init__(self, writer: AbstractWriter, factory, stream_id: int,
                 push_callback=None):
        super().__init__(writer)
        self._factory = factory
        self._stream_id = stream_id
        self._push_callback = push_callback
        self.connection_window_size = DEFAULT_INITIAL_WINDOW_SIZE
        self.stream_window_size = {stream_id: DEFAULT_INITIAL_WINDOW_SIZE}
        self.max_frame_size = DEFAULT_MAX_FRAME_SIZE
        self._window_open: asyncio.Event | None = None
        self._end_stream_sent: bool = False
        # Defer HEADERS write until first body event (mirrors HTTP1Sender).
        self._buffered_status: HTTPStatus | None = None
        self._buffered_headers: list[tuple[bytes, bytes]] | None = None
        self._expect_trailers: bool = False

    def reset_per_request_state(self) -> None:
        self._end_stream_sent = False
        self._buffered_status = None
        self._buffered_headers = None
        self._expect_trailers = False

    async def _flush_buffered_start(
        self, body: bytes, end_stream: bool,
        status: HTTPStatus, headers: list[tuple[bytes, bytes]],
        expect_trailers: bool,
    ) -> None:
        """Write buffered HEADERS + first DATA body chunk together."""
        h_frame = self._factory.create(
            FrameTypes.HEADERS,
            HeaderFrameFlags.END_HEADERS,
            self._stream_id,
        )
        h_frame.pseudo_headers[PseudoHeaders.STATUS] = str(status)
        for k, v in headers:
            h_frame.headers.append((k, v))
        if not _has_header(h_frame.headers, b'date'):
            h_frame.headers.append((b'date', _http_date()))
        h_bytes = h_frame.save()

        total = len(body)
        sid_bytes = self._stream_id.to_bytes(4, 'big')
        set_end_stream = end_stream and not expect_trailers

        if (total <= self.connection_window_size and
                total <= self.stream_window_size[self._stream_id] and
                total <= self.max_frame_size):
            end_flag = DataFrameFlags.END_STREAM if set_end_stream else 0
            if total == 0:
                d_bytes = b'\x00\x00\x00\x00' + end_flag.to_bytes(1, 'big') + sid_bytes
            else:
                d_bytes = (total.to_bytes(3, 'big') + b'\x00'
                           + end_flag.to_bytes(1, 'big') + sid_bytes + body)
            await super()._write(h_bytes + d_bytes)
            self.connection_window_size -= total
            self.stream_window_size[self._stream_id] -= total
        else:
            await super()._write(h_bytes)
            await self._write_data(body, end_stream=set_end_stream)
        if set_end_stream:
            self._end_stream_sent = True

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
        h_frame = self._factory.create(
            FrameTypes.HEADERS,
            HeaderFrameFlags.END_HEADERS,
            self._stream_id,
        )
        h_frame.pseudo_headers[PseudoHeaders.STATUS] = str(status)
        for k, v in headers:
            h_frame.headers.append((k, v))
        if not _has_header(h_frame.headers, b'date'):
            h_frame.headers.append((b'date', _http_date()))
        await self._write(h_frame.save())

    async def _write(self, data: bytes):
        """Write a frame to the transport.

        Per RFC 7540 §6.9.1, only DATA frames are subject to flow control;
        HEADERS and control frames (SETTINGS, PING, WINDOW_UPDATE, RST_STREAM,
        GOAWAY, CONTINUATION) are not.  Flow-controlled writes go through
        :meth:`_write_data`.
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
            while (self.connection_window_size <= 0 or
                   self.stream_window_size[self._stream_id] <= 0):
                if self._window_open is None:
                    self._window_open = asyncio.Event()
                self._window_open.clear()
                await self._window_open.wait()

            chunk_size = min(
                self.connection_window_size,
                self.stream_window_size[self._stream_id],
                self.max_frame_size,
                total - offset,
            )

            is_last = (offset + chunk_size >= total)
            flags = DataFrameFlags.END_STREAM if (is_last and end_stream) else 0
            chunk = body[offset:offset + chunk_size]
            await super()._write(
                chunk_size.to_bytes(3, 'big') + b'\x00' + flags.to_bytes(1, 'big') + sid_bytes + chunk
            )
            self.connection_window_size -= chunk_size
            self.stream_window_size[self._stream_id] -= chunk_size
            offset += chunk_size

    def window_update(self, increment: int) -> None:
        self.stream_window_size[self._stream_id] += increment
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
        self.stream_window_size[self._stream_id] += delta
        if delta > 0:
            self.wake_window()

    async def __call__(self, body, status: HTTPStatus = HTTPStatus.OK, headers: HeaderList = []):
        # Control-plane: raw frame object (SETTINGS, PING ACK, WINDOW_UPDATE, …)
        if isinstance(body, FrameBase):
            logger.debug('HTTP2Sender raw frame: %r', body)
            await self._write(body.save())
            return

        if isinstance(body, bytes):
            # RFC 9113 §8.1 — same defensive guard the dict branch carries.
            # If the application bytes-sends after the stream has ended,
            # drop with a warning instead of writing past END_STREAM.
            if self._end_stream_sent:
                logger.warning(
                    'HTTP2Sender: dropping bytes write on stream %d — '
                    'END_STREAM already sent (ASGI app sent a body after '
                    'the response was complete)',
                    self._stream_id)
                return
            # High-level: build HEADERS + DATA frames from bytes + status
            h_frame = self._factory.create(
                FrameTypes.HEADERS,
                HeaderFrameFlags.END_HEADERS,
                self._stream_id,
            )
            h_frame.pseudo_headers[PseudoHeaders.STATUS] = str(status)
            for k, v in headers:
                h_frame.headers.append((k, v))
            # RFC 9110 §6.6.1 — Date SHOULD be present.  HTTP/1.1 sender
            # already injects it in ``_flush``; mirror that here so the
            # wire shape matches across protocols.  Lowercase per RFC
            # 9113 §8.2.1 (HTTP/2 field names are lowercase ASCII).
            if not _has_header(h_frame.headers, b'date'):
                h_frame.headers.append((b'date', _http_date()))
            h_bytes = h_frame.save()

            total = len(body)
            sid_bytes = self._stream_id.to_bytes(4, 'big')
            if (total <= self.connection_window_size and
                    total <= self.stream_window_size[self._stream_id] and
                    total <= self.max_frame_size):
                # Fast path: body fits in one DATA frame — single write + drain
                end_flag = DataFrameFlags.END_STREAM.to_bytes(1, 'big')
                if total == 0:
                    d_bytes = b'\x00\x00\x00\x00' + end_flag + sid_bytes
                else:
                    d_bytes = total.to_bytes(3, 'big') + b'\x00' + end_flag + sid_bytes + body
                await super()._write(h_bytes + d_bytes)
                self.connection_window_size -= total
                self.stream_window_size[self._stream_id] -= total
            else:
                await super()._write(h_bytes)
                await self._write_data(body, end_stream=True)
            self._end_stream_sent = True

        elif isinstance(body, dict):
            event_type = body.get('type', '')
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
                # Buffer — defer HEADERS write until body event.
                self._buffered_status = HTTPStatus(body.get('status', 200))
                self._buffered_headers = list(body.get('headers', []))
                self._expect_trailers = bool(body.get('trailers', False))

            elif event_type == ASGIEvent.HTTP_RESPONSE_BODY:
                payload = body.get('body', b'')
                end_stream = not body.get('more_body', False)
                if self._buffered_status is not None:
                    await self._flush_buffered_start(
                        payload, end_stream, self._buffered_status,
                        self._buffered_headers, self._expect_trailers)
                    self._buffered_status = None
                    self._buffered_headers = None
                else:
                    await self._write_data(payload, end_stream=end_stream)
                if end_stream and not self._expect_trailers:
                    self._end_stream_sent = True

            elif event_type == ASGIEvent.HTTP_RESPONSE_TRAILERS:
                # Flush buffered start if no body preceded trailers.
                if self._buffered_status is not None:
                    h_frame = self._factory.create(
                        FrameTypes.HEADERS,
                        HeaderFrameFlags.END_HEADERS,
                        self._stream_id,
                    )
                    h_frame.pseudo_headers[PseudoHeaders.STATUS] = str(self._buffered_status)
                    for k, v in self._buffered_headers or ():
                        h_frame.headers.append((k, v))
                    if not _has_header(h_frame.headers, b'date'):
                        h_frame.headers.append((b'date', _http_date()))
                    await self._write(h_frame.save())
                    self._buffered_status = None
                    self._buffered_headers = None

                frame = self._factory.create(
                    FrameTypes.HEADERS,
                    HeaderFrameFlags.END_HEADERS | HeaderFrameFlags.END_STREAM,
                    self._stream_id,
                )
                for k, v in body.get('headers', []):
                    frame.headers.append((k, v))
                await self._write(frame.save())
                self._end_stream_sent = True

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

    __slots__ = ('has_received_closed', '_compressor')

    def __init__(self, writer: AbstractWriter, *, compressor=None):
        super().__init__(writer)
        self.has_received_closed = False
        # When permessage-deflate is negotiated, an
        # :class:`OutboundCompressor` is supplied here.  ``None`` means
        # outbound frames are sent verbatim (RSV1=0).
        self._compressor = compressor

    async def __call__(self, body, _status: HTTPStatus | None = None, _headers: HeaderList = []):
        if not isinstance(body, dict):
            raise TypeError(f'WebSocketSender expected a dict, got {type(body)!r}')

        event_type = body.get('type', '')

        match event_type:

            case ASGIEvent.WS_SEND:
                if 'text' in body and body['text'] is not None:
                    raw = body['text'].encode('utf-8')
                    opcode = WSOpcode.TEXT
                else:
                    raw = body.get('bytes', b'')
                    opcode = WSOpcode.BINARY
                if self._compressor is not None:
                    raw = self._compressor.compress(raw)
                    frame = encode_frame(raw, opcode=opcode, rsv1=True)
                else:
                    frame = encode_frame(raw, opcode=opcode)
                await self._write(frame)

            case ASGIEvent.WS_CLOSE:
                code = body.get('code', WSCloseCode.NORMAL)
                frame = encode_frame(code.to_bytes(2, 'big'), opcode=WSOpcode.CLOSE)
                await self._write(frame)

            case ASGIEvent.WS_ACCEPT:
                pass  # handshake reply is sent by HTTP1Actor._do_ws_handshake()
            case _:
                logger.warning('WebSocketSender: unknown event type %r', event_type)



# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

class SenderFactory:
    """Creates the appropriate BaseSender for the given protocol.

    All methods accept a raw asyncio-compatible stream writer and wrap it in
    ``AsyncioWriter`` internally.  To support a different async runtime,
    implement a new ``AbstractWriter`` subclass and pass it directly to the
    sender constructors instead.
    """

    @staticmethod
    def http1(stream_writer) -> HTTP1Sender:
        if isinstance(stream_writer, AbstractWriter):
            return HTTP1Sender(stream_writer)
        return HTTP1Sender(AsyncioWriter(stream_writer))

    @staticmethod
    def http2(stream_writer, factory, stream_id: int,
              push_callback=None) -> HTTP2Sender:
        if isinstance(stream_writer, AbstractWriter):
            return HTTP2Sender(stream_writer, factory, stream_id, push_callback)
        return HTTP2Sender(AsyncioWriter(stream_writer), factory,
                           stream_id, push_callback)

    @staticmethod
    def websocket(stream_writer, *, compressor=None) -> WebSocketSender:
        if isinstance(stream_writer, AbstractWriter):
            return WebSocketSender(stream_writer, compressor=compressor)
        return WebSocketSender(AsyncioWriter(stream_writer), compressor=compressor)
