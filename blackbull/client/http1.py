"""HTTP/1.1 client (RFC 7230).

Provides ``HTTP1Client`` plus the lower-level ``HTTP1RequestSender`` /
``HTTP1ResponseRecipient`` helpers that frame and unframe HTTP/1.1 messages
on the wire.

Symmetric with the server-side ``HTTP1Sender`` / ``HTTP1Recipient`` in
:mod:`blackbull.server.sender` and :mod:`blackbull.server.recipient`,
but reversed: the client *writes* request lines + request headers + request
body, and *reads* status lines + response headers + response body.
"""
import asyncio
import ssl as _ssl
from collections.abc import AsyncIterable, AsyncIterator
from http import HTTPMethod
from typing import Union

import logging
from ..headers import Headers, HeaderList
from ..server.recipient import (AbstractReader, AsyncioReader,
                                IncompleteReadError)
from ..server.sender import AbstractWriter, AsyncioWriter
from .exceptions import ConnectionError, ProtocolError
from .http2 import ClientResponse  # shared dataclass
from blackbull.fault_injection.scenario_h1 import (
    Abort,
    ReadResponse,
    Scenario,
    ScenarioResult,
    SendBytes,
    Sleep,
)

logger = logging.getLogger(__name__)


# Type for request bodies — either a complete byte string or an async iterable
# of byte chunks (the streaming case, encoded as Transfer-Encoding: chunked).
RequestBody = Union[bytes, bytearray, memoryview, AsyncIterable[bytes]]

# CRLF as used throughout RFC 7230.
_CRLF = b'\r\n'

# Sprint 17 Phase 3 — methods for which an empty body still warrants an
# explicit ``Content-Length: 0`` on the wire.  RFC 9110 §8.6 makes the
# header optional in this case, but always emitting it removes ambiguity
# for upstreams (notably reverse proxies that treat absent CL on POST as
# "read body until close").  Methods listed in BODY_LESS_METHODS instead
# skip the header entirely when no body is present, matching nginx /
# uvicorn / curl conventions.
_BODY_ALLOWED_METHODS = frozenset({b'POST', b'PUT', b'PATCH', b'DELETE'})
_BODY_LESS_METHODS = frozenset({b'GET', b'HEAD', b'OPTIONS', b'TRACE', b'CONNECT'})


class HTTP1RequestSender:
    """Writes an HTTP/1.1 request — request line, headers, body — to an ``AbstractWriter``.

    Adds ``Content-Length`` automatically for fixed-size byte bodies; switches
    to ``Transfer-Encoding: chunked`` for ``AsyncIterable`` bodies.  The
    ``Host`` header MUST be present (RFC 7230 §5.4) — the helper raises
    ``ProtocolError`` if it is not.
    """

    def __init__(self, writer: AbstractWriter) -> None:
        self._writer = writer

    async def send(self, method: str | HTTPMethod, path: str,
                   headers: Headers, body: RequestBody = b'') -> None:
        if b'host' not in headers:
            raise ProtocolError('HTTP/1.1 request requires a Host header')

        if isinstance(body, (bytes, bytearray, memoryview)):
            await self._send_fixed(str(method), path, headers, bytes(body))
        else:
            await self._send_chunked(str(method), path, headers, body)

    async def _send_fixed(self, method: str, path: str, headers: Headers,
                          body: bytes) -> None:
        self._normalize_content_length(method, headers, body)
        await self._write_start(method, path, headers)
        if body:
            await self._writer.write(body)

    @staticmethod
    def _normalize_content_length(method: str, headers: Headers,
                                  body: bytes) -> None:
        """Emit / validate the ``Content-Length`` header for the request.

        Sprint 17 Phase 3 — replaces the previous "add CL only if body is
        truthy and no CL already present" rule, which let an empty POST go
        out without any framing header at all (RFC-legal but confuses
        reverse proxies) and silently sent inconsistent framing if the
        caller pre-passed a CL that didn't match the body.

        New rules:
          * If the caller pre-passed a ``Content-Length`` header, verify
            it parses as an int and equals ``len(body)``.  Raise
            ``ValueError`` on mismatch — duplicate / mismatched CL is a
            CL.CL smuggling vector and the right thing to do is fail
            loudly, not silently send something the peer will reject.
          * Otherwise, for body-allowed methods (POST/PUT/PATCH/DELETE),
            always emit ``Content-Length: N`` — including ``0`` for an
            empty body.  This matches nginx/curl and avoids absent-CL
            ambiguity.
          * For body-less methods (GET/HEAD/OPTIONS/TRACE/CONNECT) with
            an empty body, skip the header entirely.  An empty GET should
            not carry ``Content-Length: 0`` because some upstream proxies
            treat that as suspicious.
          * For unknown / custom methods, fall back to the "emit when
            body is non-empty" rule — the safe path for forwards
            compatibility.
        """
        cl_actual = len(body)
        method_upper = method.upper().encode() if isinstance(method, str) else method.upper()

        if b'content-length' in headers:
            cl_existing = headers.get(b'content-length')
            try:
                cl_int = int(cl_existing)
            except ValueError as exc:
                raise ValueError(
                    f'caller-supplied Content-Length is not an integer: '
                    f'{cl_existing!r}'
                ) from exc
            if cl_int != cl_actual:
                raise ValueError(
                    f'caller-supplied Content-Length ({cl_int}) does not '
                    f'match body length ({cl_actual} bytes); rejecting '
                    f'to avoid CL.CL smuggling on the wire'
                )
            return

        # No caller-supplied Content-Length — decide whether to add one.
        if cl_actual > 0:
            headers.append(b'content-length', str(cl_actual).encode())
            return
        if method_upper in _BODY_ALLOWED_METHODS:
            # Empty body on a body-allowed method — still emit CL: 0.
            headers.append(b'content-length', b'0')
            return
        if method_upper in _BODY_LESS_METHODS:
            # GET/HEAD/etc. with no body — skip the header by design.
            return
        # Unknown method: forwards-compatible fallback identical to the
        # pre-Sprint-17 behaviour (omit CL on empty body).
        return

    async def _send_chunked(self, method: str, path: str, headers: Headers,
                            body: AsyncIterable[bytes]) -> None:
        if b'transfer-encoding' not in headers:
            headers.append(b'transfer-encoding', b'chunked')
        await self._write_start(method, path, headers)
        async for chunk in body:
            if chunk:
                await self._writer.write(
                    f'{len(chunk):x}'.encode() + _CRLF + chunk + _CRLF)
        await self._writer.write(b'0' + _CRLF + _CRLF)

    async def _write_start(self, method: str, path: str, headers: Headers) -> None:
        chunks: list[bytes] = [f'{method} {path} HTTP/1.1'.encode() + _CRLF]
        for k, v in headers:
            chunks.append(k + b': ' + v + _CRLF)
        chunks.append(_CRLF)
        await self._writer.write(b''.join(chunks))


class HTTP1ResponseRecipient:
    """Reads an HTTP/1.1 response from an ``AbstractReader``.

    Decodes both ``Content-Length``-bound and ``Transfer-Encoding: chunked``
    bodies.  Returns a ``ClientResponse``; ``stream()`` returns an async
    iterator of body chunks instead, so large responses don't have to fit
    in memory.
    """

    async def receive(self, reader: AbstractReader) -> ClientResponse:
        status, headers = await self._read_start(reader)
        body = await self._read_body(reader, headers)
        return ClientResponse(status=status, headers=headers, body=body)

    async def stream(self, reader: AbstractReader) -> AsyncIterator[bytes]:
        # Body-only streaming: callers that need status/headers should use
        # ``receive``.  Yielding the start-line as the first item would force
        # callers to special-case the iterator's first element.
        _, headers = await self._read_start(reader)
        async for chunk in self._stream_body(reader, headers):
            yield chunk

    async def _read_start(self, reader: AbstractReader) -> tuple[int, Headers]:
        try:
            status_line = await reader.readuntil(_CRLF)
        except IncompleteReadError as exc:
            raise ConnectionError('connection closed before response') from exc
        # "HTTP/1.1 200 OK\r\n" — split into version, status, reason.
        parts = status_line.rstrip(_CRLF).split(b' ', 2)
        if len(parts) < 2:
            raise ProtocolError(f'malformed status line: {status_line!r}')
        try:
            status = int(parts[1])
        except ValueError as exc:
            raise ProtocolError(f'invalid status code: {parts[1]!r}') from exc

        pairs: list[tuple[bytes, bytes]] = []
        while True:
            line = await reader.readuntil(_CRLF)
            if line == _CRLF:
                break
            name, _, value = line.rstrip(_CRLF).partition(b':')
            pairs.append((name.strip().lower(), value.strip()))
        return status, Headers(pairs)

    async def _read_body(self, reader: AbstractReader, headers: Headers) -> bytes:
        te = headers.get(b'transfer-encoding', b'').lower()
        if te == b'chunked':
            return b''.join([c async for c in self._read_chunked(reader)])
        cl_raw = headers.get(b'content-length')
        if cl_raw is not None:
            return await reader.readexactly(int(cl_raw))
        # No Content-Length and no chunked: caller (e.g. HEAD, 204, 304) must
        # interpret as empty body.  We don't read-until-EOF here because that
        # would break keep-alive.
        return b''

    async def _stream_body(self, reader: AbstractReader,
                           headers: Headers) -> AsyncIterator[bytes]:
        te = headers.get(b'transfer-encoding', b'').lower()
        if te == b'chunked':
            async for chunk in self._read_chunked(reader):
                yield chunk
            return
        cl_raw = headers.get(b'content-length')
        if cl_raw is not None:
            yield await reader.readexactly(int(cl_raw))

    async def _read_chunked(self, reader: AbstractReader) -> AsyncIterator[bytes]:
        while True:
            size_line = await reader.readuntil(_CRLF)
            try:
                size = int(size_line.rstrip(_CRLF).split(b';', 1)[0], 16)
            except ValueError as exc:
                raise ProtocolError(f'invalid chunk size: {size_line!r}') from exc
            if size == 0:
                # Read trailing CRLF after the terminator chunk; ignore any
                # trailers (RFC 7230 §4.1.2) — they are rarely used.
                await reader.readuntil(_CRLF)
                return
            chunk = await reader.readexactly(size)
            await reader.readuntil(_CRLF)  # consume CRLF after chunk data
            yield chunk


class HTTP1Client:
    """Async HTTP/1.1 client.

    Use as an async context manager::

        async with HTTP1Client('localhost', 8000) as c:
            res = await c.request(HTTPMethod.GET, '/path')

    The connection persists across multiple ``request()`` calls (HTTP/1.1
    persistent connections, RFC 7230 §6.3) until ``__aexit__`` closes it.
    Pass ``ssl=`` to use TLS.

    The ``Host`` header is injected automatically when the caller omits it.
    """

    def __init__(self, host: str, port: int, *,
                 ssl: _ssl.SSLContext | None = None,
                 record_wire_bytes: bool = False,
                 connect_timeout: float | None = None) -> None:
        self._host = host
        self._port = port
        self._ssl = ssl
        self._reader: AbstractReader | None = None
        self._writer: AbstractWriter | None = None
        self._raw_writer: asyncio.StreamWriter | None = None
        # Sprint 17 Phase 2 — opt-in wire-bytes capture for the low-level
        # primitives (send_raw, send_request_line, send_header_line, ...).
        # When True, every byte sent through those methods is also appended
        # to ``self._wire_buffer`` for later inspection by failing tests.
        # request() does NOT populate this buffer in Sprint 17; capture
        # there is a Phase 8 concern.
        self._record_wire_bytes = record_wire_bytes
        self._wire_buffer: bytearray = bytearray()
        # Sprint 17 Phase 5 — bound the connect() so a hung accept can't
        # stall the scenario executor before any step runs.  atheris in
        # particular cannot afford an unbounded wait per TestOneInput.
        # None = no timeout (preserves pre-Phase-5 behaviour).
        self._connect_timeout = connect_timeout

    # ---- async context manager -------------------------------------------

    async def __aenter__(self) -> 'HTTP1Client':
        if self._raw_writer is None:
            coro = asyncio.open_connection(self._host, self._port, ssl=self._ssl)
            if self._connect_timeout is not None:
                r, w = await asyncio.wait_for(coro, timeout=self._connect_timeout)
            else:
                r, w = await coro
            self._raw_writer = w
            self._reader = AsyncioReader(r)
            self._writer = AsyncioWriter(w)
        await self._start()
        return self

    @classmethod
    def _adopt(cls, host: str, port: int,
               reader: asyncio.StreamReader, writer: asyncio.StreamWriter,
               *, ssl: _ssl.SSLContext | None = None) -> 'HTTP1Client':
        """Wrap an already-open ``(reader, writer)`` pair as an HTTP1Client.

        Used by ``Client`` (the ALPN dispatcher) to hand off a TLS-handshaken
        connection without re-opening the transport.
        """
        c = cls(host, port, ssl=ssl)
        c._raw_writer = writer
        c._reader = AsyncioReader(reader)
        c._writer = AsyncioWriter(writer)
        return c

    async def _start(self) -> None:
        """Per-protocol post-connect initialisation hook.

        HTTP/1.1 has no preface or persistent loop, so this is a no-op; it
        exists for API symmetry with ``HTTP2Client._start``.
        """
        return None

    async def __aexit__(self, exc_type, exc, tb) -> None:
        if self._raw_writer is not None:
            try:
                self._raw_writer.close()
                await self._raw_writer.wait_closed()
            except Exception:
                pass  # best-effort close on context exit; peer may already be gone.

    # ---- public API ------------------------------------------------------

    async def request(self, method: str | HTTPMethod, path: str, *,
                      headers: HeaderList = (),
                      body: RequestBody = b'') -> ClientResponse:
        assert self._writer is not None and self._reader is not None
        h = self._headers_with_host(headers)
        await HTTP1RequestSender(self._writer).send(method, path, h, body)
        return await HTTP1ResponseRecipient().receive(self._reader)

    async def stream(self, method: str | HTTPMethod, path: str, *,
                     headers: HeaderList = (),
                     body: RequestBody = b'') -> AsyncIterator[bytes]:
        """Send a request and yield body chunks lazily.

        Unlike ``request()`` this does not buffer the response body, so
        gigabyte-sized responses do not need to fit in memory.  Status and
        headers are not exposed by this method; use ``request()`` if you
        need them.
        """
        assert self._writer is not None and self._reader is not None
        h = self._headers_with_host(headers)
        await HTTP1RequestSender(self._writer).send(method, path, h, body)
        recipient = HTTP1ResponseRecipient()
        async for chunk in recipient.stream(self._reader):
            yield chunk

    def _headers_with_host(self, headers: HeaderList) -> Headers:
        h = Headers(list(headers))
        if b'host' not in h:
            h.append(b'host', f'{self._host}:{self._port}'.encode())
        return h

    # ---- Sprint 17 Phase 2 — low-level test-instrument primitives ----
    #
    # These methods bypass HTTP1RequestSender's safety net (no Host
    # injection, no Content-Length, no validation).  They exist so tests
    # can put deliberately malformed bytes on the wire — slowloris-style
    # trickle, duplicate Content-Length, invalid request lines, etc. —
    # without dropping to a raw asyncio socket and duplicating wire-
    # shaping logic.  The high-level request() / stream() API is the
    # production path and remains source-compatible.

    @property
    def wire_buffer(self) -> bytes:
        """Bytes sent so far by the low-level primitives in this session.

        Empty unless the client was constructed with
        ``record_wire_bytes=True``.  Reset with :meth:`reset_wire_buffer`.
        """
        return bytes(self._wire_buffer)

    def reset_wire_buffer(self) -> None:
        """Discard previously captured wire bytes."""
        self._wire_buffer.clear()

    async def send_raw(self, data: bytes, *,
                       byte_interval: float = 0.0) -> None:
        """Push arbitrary bytes onto the underlying socket.

        When ``byte_interval > 0`` the bytes are transmitted one at a time
        with ``byte_interval`` seconds between writes — the primitive
        slowloris-style stall the differential tests rely on.  Each per-
        byte write is followed by ``drain()`` (inherited from
        :class:`AsyncioWriter`), so the bytes actually leave the socket
        on schedule rather than accumulating in the asyncio send buffer.
        """
        assert self._writer is not None, 'connect via __aenter__ first'
        if self._record_wire_bytes:
            self._wire_buffer.extend(data)
        if byte_interval <= 0.0 or len(data) <= 1:
            await self._writer.write(data)
            return
        for i, byte in enumerate(data):
            await self._writer.write(bytes((byte,)))
            if i + 1 < len(data):
                await asyncio.sleep(byte_interval)

    async def send_request_line(
        self, method: bytes | str | HTTPMethod,
        target: bytes | str,
        *, version: bytes = b'HTTP/1.1',
    ) -> None:
        """Emit ``METHOD<SP>TARGET<SP>HTTP/1.1\\r\\n`` with no validation.

        Accepts arbitrary bytes for ``method``/``target``/``version`` so a
        test can deliberately send ``b"BREW"``, lowercase versions, or
        garbage tokens.  No automatic Host or Content-Length injection —
        the caller drives the wire bit by bit.
        """
        m: bytes
        if isinstance(method, HTTPMethod):
            m = str(method).encode()
        elif isinstance(method, str):
            m = method.encode()
        else:
            m = bytes(method)
        t: bytes = target.encode() if isinstance(target, str) else bytes(target)
        await self.send_raw(m + b' ' + t + b' ' + version + _CRLF)

    async def send_header_line(self, name: bytes, value: bytes) -> None:
        """Emit one ``Name: Value\\r\\n`` header line with no dedup or
        validation.  Callers wanting a duplicate ``Content-Length`` or a
        header value containing arbitrary bytes use this primitive
        directly."""
        await self.send_raw(name + b': ' + value + _CRLF)

    async def end_headers(self) -> None:
        """Emit the bare CRLF that terminates the header block."""
        await self.send_raw(_CRLF)

    async def send_body_bytes(self, data: bytes, *,
                              byte_interval: float = 0.0) -> None:
        """Send body octets to the peer.

        Same semantics as :meth:`send_raw`, kept separate for readability
        at call sites that frame headers separately from the body."""
        await self.send_raw(data, byte_interval=byte_interval)

    async def send_chunk(self, data: bytes) -> None:
        """Send one ``Transfer-Encoding: chunked`` chunk.

        Caller must have already emitted ``Transfer-Encoding: chunked``
        via :meth:`send_header_line` and called :meth:`end_headers`.
        Finish the chunked stream with :meth:`end_chunked`."""
        await self.send_raw(f'{len(data):x}'.encode() + _CRLF + data + _CRLF)

    async def end_chunked(self) -> None:
        """Emit the size-0 terminator chunk that closes a chunked body."""
        await self.send_raw(b'0' + _CRLF + _CRLF)

    async def read_response(self, *,
                            timeout: float | None = None) -> ClientResponse:
        """Read one HTTP/1.1 response from the connection.

        Optional ``timeout`` bounds the entire read (status line + headers
        + body).  Raises :class:`asyncio.TimeoutError` if the deadline is
        hit; the caller decides whether to treat that as a transport-
        fail or a normal protocol outcome."""
        assert self._reader is not None, 'connect via __aenter__ first'
        coro = HTTP1ResponseRecipient().receive(self._reader)
        if timeout is None:
            return await coro
        return await asyncio.wait_for(coro, timeout=timeout)

    # ---- Sprint 17 Phase 5 — scenario executor ----------------------------
    #
    # A :class:`Scenario` is a tagged sequence of steps (SendBytes / Sleep /
    # ReadResponse / Abort) shared by the differential test and the atheris
    # fuzz harness.  The executor below is glue over the Phase 2 primitives:
    # it walks the steps, dispatches each to the appropriate primitive, and
    # folds the outcome into a :class:`ScenarioResult` without raising.

    async def execute_scenario(
        self, scenario: Scenario,
    ) -> ScenarioResult:
        """Walk ``scenario.steps`` against the connected socket.

        Never raises.  Every outcome (response, timeout, transport
        failure, hard-abort) is folded into the returned
        :class:`ScenarioResult` so callers can categorise without
        try/except boilerplate per scenario.

        Step dispatch:
          * :class:`SendBytes`   → :meth:`send_raw`
          * :class:`Sleep`       → :func:`asyncio.sleep`
          * :class:`ReadResponse` → :meth:`read_response`
          * :class:`Abort`       → ``transport.abort()`` (RST on Linux);
                                   walks no further steps.
        """
        import time as _time  # noqa: PLC0415

        # Per-step primitives assert on their own preconditions
        # (send_raw needs _writer, read_response needs _reader), so we
        # don't gate the executor on _reader here — scenarios that
        # never read shouldn't have to wire a reader.
        assert self._writer is not None, 'connect via __aenter__ first'
        result = ScenarioResult()
        t0 = _time.monotonic()
        try:
            for step in scenario.steps:
                if isinstance(step, SendBytes):
                    await self.send_raw(step.data, byte_interval=step.byte_interval)
                elif isinstance(step, Sleep):
                    await asyncio.sleep(step.duration)
                elif isinstance(step, ReadResponse):
                    try:
                        result.response = await self.read_response(timeout=step.timeout)
                    except asyncio.TimeoutError as exc:
                        result.timed_out = True
                        result.exception = repr(exc)
                        return result
                elif isinstance(step, Abort):
                    # Hard-close: send RST rather than FIN.  abort() is
                    # synchronous on asyncio's transport layer.  After
                    # this, subsequent socket I/O would raise; short-
                    # circuit by returning.
                    if self._raw_writer is not None:
                        self._raw_writer.transport.abort()
                    result.aborted = True
                    return result
                else:  # noqa: PLR5501
                    raise TypeError(f'unknown step type: {type(step).__name__}')
                result.steps_completed += 1
        except Exception as exc:  # noqa: BLE001
            result.exception = repr(exc)
        finally:
            result.elapsed_s = _time.monotonic() - t0
        return result
