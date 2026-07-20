import asyncio
import os
import time
from abc import ABC, abstractmethod
from http import HTTPStatus
from email.utils import formatdate
from itertools import chain

from ..protocol import hpack_fastpath
from ..protocol.frame_types import (FrameTypes, HeaderFrameFlags, DataFrameFlags,
                                    FrameBase, PseudoHeaders,
                                    DEFAULT_INITIAL_WINDOW_SIZE, DEFAULT_MAX_FRAME_SIZE)
from .cap_log import log_cap_hit
from .constants import WSCloseCode
from .ws_codec import WSOpcode, encode_frame, encode_frame_header
import logging
from ..asgi import ASGIEvent
from ..headers import Headers, HeaderList

logger = logging.getLogger(__name__)

_CRLF = b'\r\n'

# Fallback chunk size when ``sendfile`` isn't supported by the transport
# (TLS, mocked tests).  Matches the static middleware's ``_CHUNK`` so
# memory-peak guarantees stay consistent across paths.
_PATHSEND_FALLBACK_CHUNK = 64 * 1024

# ``BaseSender._write_many`` size gate: parts totalling at most this many
# bytes are joined and sent via ``write()``; larger payloads use vectored
# ``writelines``.  Breakeven measured on a drained socketpair (selector
# transport, 2026-07-12 protocol-layer audit): join wins ≤ 16 KiB, vectored
# wins ≥ 64 KiB — and HttpArena's 17 KiB static lanes regressed under
# ``writelines``, so the crossover sits at 32 KiB.  Deliberately NOT a
# Settings knob (audit: no configuration surface without deployment data).
_VECTORED_JOIN_THRESHOLD = 32 * 1024


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
# Response-HEADERS fast-path builders (frame-assembly-fast-path Tier 2)
# ---------------------------------------------------------------------------
#
# These encode a HEADERS frame straight to wire bytes, skipping
# ``FrameFactory.create()``, the registry lookup, and the receive-oriented
# ``Headers`` object — none of whose parsing machinery the send side reads.
# They are wire-equivalent to ``Headers.save()`` for the response path:
#  - the same shared per-connection HPACK ``Encoder`` is used, so dynamic
#    table state stays coherent with every other emitter on the connection;
#  - ``status_fast_bytes`` is the same static-table fast path ``Headers.save()``
#    already uses (RFC 7541 §6.1 — static-indexed fields don't touch the
#    dynamic table), just hoisted out of the object;
#  - the 9-byte frame header is byte-identical to ``FrameBase.save()``.
# Verified byte-for-byte against ``Headers.save()`` in
# ``tests/conformance/http2/test_headers_fastpath_builder.py``.
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

    async def _drain_with_timeout(self) -> None:
        """Drain the underlying StreamWriter, bounded by ``_write_timeout``.

        On timeout, close the transport (so the FD/connection slot is
        reclaimed from a slow-read peer or dead TCP route) and surface a
        ``ConnectionResetError`` so the sender's existing peer-disconnect
        handling runs uniformly.  When no timeout is configured this is a
        plain ``drain()``.
        """
        if self._write_timeout <= 0:
            await self._sw.drain()
            return
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
                # Best-effort transport teardown.  We're already in the
                # timeout error path and the transport may be half-broken
                # (SSL aborted, FD already reaped by a sibling task, etc.);
                # swallowing here lets us still raise ConnectionResetError
                # below so the peer-disconnect handling runs uniformly.
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
    async def __call__(self, body, status: HTTPStatus = HTTPStatus.OK, headers: HeaderList = []): pass

    async def _guarded_write(self, write_fn, arg) -> None:
        """Run *write_fn(arg)* tolerant of peer-closed transports.

        Once a write hits ``ConnectionResetError`` / ``BrokenPipeError`` /
        SSL EOF, the sender marks itself closed and subsequent writes
        silently drop.  These exceptions used to propagate out as
        tracebacks under wrk c=1024 sustained load — 22 per 30 s in the
        141848 run.

        ``_closed`` is bound in ``__init__`` for every sender, so a direct
        attribute read is safe (and cheaper than the old ``getattr`` guard)
        on this per-write hot path.
        """
        if self._closed:
            return
        try:
            await write_fn(arg)
        except (ConnectionResetError, BrokenPipeError) as exc:
            self._closed = True
            logger.debug('sender: peer closed write side (%s)', exc.__class__.__name__)
        except OSError as exc:
            # SSLEOFError / SSLZeroReturnError land here on TLS connections
            # whose peer dropped without a proper close-notify.
            self._closed = True
            logger.debug('sender: write failed on closed TLS transport (%s)', exc.__class__.__name__)

    async def _write(self, data: bytes):
        """Flush *data* through the writer (peer-close tolerant)."""
        await self._guarded_write(self._writer.write, data)

    async def _write_many(self, parts) -> None:
        """Write *parts* (peer-close tolerant), choosing join vs vectored I/O.

        Protocol senders call this to express *what* they have — a response
        that naturally exists as separate fragments (``(head, body)``,
        ``(frame_header, payload)``) — and this method owns *how* to send it:

        * total ≤ :data:`_VECTORED_JOIN_THRESHOLD` — join and ``write()``.
          On CPython's selector transport ``writelines`` costs more than the
          small memcpy it avoids (per-part ``memoryview`` allocations and
          ``sendmsg`` setup; under backpressure it also attempts a send and
          re-registers the writer on every call, where ``write()`` merely
          appends).  This was the v0.33.1 → v0.51.0 HttpArena regression
          (echo-ws −8~−20 %, HTTP/1.1 −4~−8 %); see the 2026-07-12
          protocol-layer audit.
        * total > threshold — vectored ``writelines``: skipping the
          full-body memcpy wins on the static-file cache-hit path.
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
        '_completed',
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
        # Set True once a complete response has been written for this request
        # (a full ``bytes`` response, a body event with ``more_body=False``,
        # trailers, or a pathsend).  Further response events are then dropped
        # (bug 1.4) so a handler that raises *after* completing its response
        # can't write a second one onto the same keep-alive connection —
        # mirrors the H2 sender's post-END_STREAM drop.
        self._completed: bool = False
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
        if self._completed:
            # bug 1.4 — a complete response has already gone out for this
            # request; drop any further response events so a handler that
            # raises after completing (→ the error handler emits a second
            # response) can't splice two responses onto one connection.
            # A disconnect signal still marks the writer closed.
            if isinstance(body, dict) and body.get('type') == ASGIEvent.HTTP_DISCONNECT:
                self._closed = True
            return

        match body:
            case bytes():
                h = headers if isinstance(headers, Headers) else Headers(headers)
                if self._log_record is not None:
                    self._log_record.status = int(status)
                    self._log_record.response_bytes += len(body)
                await self._flush(status, h, body)
                self._completed = True

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
                        if not more_body:
                            self._completed = True
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
                if not more_body:
                    self._completed = True

            case {'type': ASGIEvent.HTTP_RESPONSE_TRAILERS}:
                await self._write(b'0\r\n')
                for name, value in body.get('headers', []):
                    await self._write(name + b': ' + value + b'\r\n')
                await self._write(b'\r\n')
                self._completed = True

            case {'type': ASGIEvent.HTTP_RESPONSE_PATHSEND}:
                await self._pathsend(body['path'])
                self._completed = True

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
        self._completed = False
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


class ConnectionWindow:
    """Shared HTTP/2 connection-level (stream 0) send flow-control window.

    One instance per connection, referenced by every stream's
    :class:`HTTP2Sender`, so all senders debit and await a single budget.

    Without sharing (bug 1.2) each sender held a *private copy* of the
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
        '_conn_window', 'stream_window_size',
        'max_frame_size', '_window_open', '_end_stream_sent',
        '_buffered_status', '_buffered_headers', '_expect_trailers',
        '_buffered_body',
    )

    def __init__(self, writer: AbstractWriter, factory, stream_id: int,
                 push_callback=None,
                 conn_window: 'ConnectionWindow | None' = None,
                 initial_window: int | None = None):
        super().__init__(writer)
        self._factory = factory
        self._stream_id = stream_id
        self._push_callback = push_callback
        # Shared connection-level send window (bug 1.2).  The server passes one
        # ``ConnectionWindow`` instance to every stream sender so they debit a
        # single budget; when omitted (the experimental client, whose per-sender
        # windowing is bug 1.20a and deferred) each sender gets a private one,
        # preserving today's behaviour there.
        self._conn_window = conn_window if conn_window is not None else ConnectionWindow()
        # Per-stream send window.  Refactor 2.5 — a plain int (this was a
        # dict-of-one keyed on the sender's own stream id, which obscured that
        # it is scalar and invited readers to hunt for multi-stream semantics
        # that never existed).  Seeded at construction from the peer's
        # SETTINGS_INITIAL_WINDOW_SIZE when known (bugs 1.20a + 2.11): both
        # server and client pass ``initial_window`` so a sender created after
        # the SETTINGS exchange starts at the peer's announced window, not
        # the RFC 9113 §6.9.2 default.
        self.stream_window_size = (DEFAULT_INITIAL_WINDOW_SIZE
                                   if initial_window is None else initial_window)
        self.max_frame_size = DEFAULT_MAX_FRAME_SIZE
        self._window_open: asyncio.Event | None = None
        self._end_stream_sent: bool = False
        # Defer HEADERS write until first body event (mirrors HTTP1Sender).
        self._buffered_status: HTTPStatus | None = None
        self._buffered_headers: list[tuple[bytes, bytes]] | None = None
        self._expect_trailers: bool = False
        # When trailers are expected, the first single-frame body chunk is held
        # here so HEADERS + DATA + trailing HEADERS coalesce into one write at
        # the trailers event (the unary-gRPC pattern).  Flushed early if a
        # second body chunk arrives (multi-frame body / streaming).
        self._buffered_body: bytes | None = None

    @property
    def connection_window_size(self) -> int:
        """The shared connection-level send window (bug 1.2).

        Proxies :attr:`ConnectionWindow.size` so existing call sites — the
        experimental client's per-sender crediting and flow-control tests —
        keep reading/writing ``sender.connection_window_size`` while the real
        state lives on the shared object.
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

    async def _flush_buffered_start(
        self, body: bytes, end_stream: bool,
        status: HTTPStatus, headers: list[tuple[bytes, bytes]],
        expect_trailers: bool,
    ) -> None:
        """Write buffered HEADERS + first DATA body chunk together."""
        # HEADERS never carries END_STREAM here — an empty DATA frame does
        # (mirrors the object path this replaced).
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
            await super()._write(h_bytes + d_bytes)
            self._conn_window.size -= total
            self.stream_window_size -= total
        else:
            await super()._write(h_bytes)
            await self._write_data(body, end_stream=set_end_stream)
        if set_end_stream:
            self._end_stream_sent = True

    def _auto_flush_buffered_body(self) -> None:
        """``call_soon`` callback — if the buffered body hasn't been consumed
        by a synchronous trailers or second-body event, flush it now.

        ``call_soon`` callbacks fire at the start of the next event-loop
        iteration, *after* any synchronous ASGI events emitted in the same
        coroutine.  This gives trailers (or a second body chunk) a chance to
        coalesce before the auto-flush fires."""
        if self._buffered_body is None:
            return
        asyncio.ensure_future(self._do_auto_flush())

    async def _do_auto_flush(self) -> None:
        """Flush the buffered body + headers.  Called as a task when the
        event loop detects the producer has parked (no synchronous trailers
        or second body arrived within the same event-loop iteration)."""
        body = self._buffered_body
        status = self._buffered_status
        headers = self._buffered_headers
        expect = self._expect_trailers
        if body is None or status is None:
            return
        self._buffered_body = None
        self._buffered_status = None
        self._buffered_headers = None
        await self._flush_buffered_start(body, False, status, headers, expect)

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
                await self._window_open.wait()

            chunk_size = min(
                self._conn_window.size,
                self.stream_window_size,
                self.max_frame_size,
                total - offset,
            )

            is_last = (offset + chunk_size >= total)
            flags = DataFrameFlags.END_STREAM if (is_last and end_stream) else 0
            chunk = body[offset:offset + chunk_size]
            await super()._write(
                chunk_size.to_bytes(3, 'big') + b'\x00' + flags.to_bytes(1, 'big') + sid_bytes + chunk
            )
            self._conn_window.size -= chunk_size
            self.stream_window_size -= chunk_size
            offset += chunk_size

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
            # High-level: build HEADERS + DATA frames from bytes + status.
            # RFC 9110 §6.6.1 — Date SHOULD be present; the builder injects
            # it when the app didn't (mirrors the HTTP/1.1 _flush path).
            # END_STREAM always rides the DATA frame below, never HEADERS.
            h_bytes = build_response_headers(
                self._factory.encoder, self._stream_id, status, headers,
                end_stream=False)

            total = len(body)
            sid_bytes = self._stream_id.to_bytes(4, 'big')
            if (total <= self._conn_window.size and
                    total <= self.stream_window_size and
                    total <= self.max_frame_size):
                # Fast path: body fits in one DATA frame — single write + drain
                end_flag = DataFrameFlags.END_STREAM.to_bytes(1, 'big')
                if total == 0:
                    d_bytes = b'\x00\x00\x00\x00' + end_flag + sid_bytes
                else:
                    d_bytes = total.to_bytes(3, 'big') + b'\x00' + end_flag + sid_bytes + body
                await super()._write(h_bytes + d_bytes)
                self._conn_window.size -= total
                self.stream_window_size -= total
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
                        asyncio.get_running_loop().call_soon(
                            self._auto_flush_buffered_body)
                    else:
                        # A second body chunk (or a multi-frame / terminal one):
                        # flush any deferred first chunk with the HEADERS, then
                        # write this chunk normally.
                        if self._buffered_body is not None:
                            await self._flush_buffered_start(
                                self._buffered_body, False, self._buffered_status,
                                self._buffered_headers, self._expect_trailers)
                            self._buffered_status = None
                            self._buffered_headers = None
                            self._buffered_body = None
                            await self._write_data(payload, end_stream=end_stream)
                        else:
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
                # HPACK's dynamic table is stateful, so header blocks MUST be
                # encoded in wire order: the response HEADERS block first, then
                # the trailing HEADERS block.  Encoding trailers before the
                # deferred HEADERS would desync the peer's HPACK decoder.
                if self._buffered_status is not None:
                    # Start (and possibly one deferred body chunk) never flushed:
                    # emit HEADERS [+ DATA] + trailing HEADERS in a single write.
                    h_bytes = build_response_headers(
                        self._factory.encoder, self._stream_id,
                        self._buffered_status, self._buffered_headers or [],
                        end_stream=False)
                    trailer_bytes = build_trailers(
                        self._factory.encoder, self._stream_id,
                        list(body.get('headers', [])))
                    if self._buffered_body is not None:
                        total = len(self._buffered_body)
                        d_bytes = (total.to_bytes(3, 'big') + b'\x00'
                                   + b'\x00'  # DATA flags: no END_STREAM (trailers carry it)
                                   + self._stream_id.to_bytes(4, 'big')
                                   + self._buffered_body)
                        await self._write(h_bytes + d_bytes + trailer_bytes)
                        self._conn_window.size -= total
                        self.stream_window_size -= total
                        self._buffered_body = None
                    else:
                        await self._write(h_bytes + trailer_bytes)
                    self._buffered_status = None
                    self._buffered_headers = None
                else:
                    await self._write(build_trailers(
                        self._factory.encoder, self._stream_id,
                        list(body.get('headers', []))))
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
                rsv1 = self._compressor is not None
                if rsv1:
                    raw = self._compressor.compress(raw)
                # Vectored write: hand (header, payload) to writelines so the
                # payload is never copied into a concatenated frame buffer
                # (the header+payload join encode_frame would allocate).
                header = encode_frame_header(len(raw), opcode, rsv1=rsv1)
                await self._write_many((header, raw))

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
              initial_window: int | None = None) -> HTTP2Sender:
        return HTTP2Sender(SenderFactory._ensure_writer(stream_writer),
                           factory, stream_id, push_callback,
                           conn_window=conn_window,
                           initial_window=initial_window)

    @staticmethod
    def websocket(stream_writer, *, compressor=None) -> WebSocketSender:
        return WebSocketSender(SenderFactory._ensure_writer(stream_writer),
                               compressor=compressor)
