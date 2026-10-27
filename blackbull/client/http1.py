"""HTTP/1.1 request framing and response reading (RFC 9110, RFC 9112).
"""
import asyncio
import enum
import ssl as _ssl
from collections.abc import AsyncIterable, AsyncIterator
from time import monotonic as _monotonic
from http import HTTPMethod
from typing import NamedTuple, Union

import logging
from ..env import get_settings
from ..headers import Headers, HeaderList
from ..server.cap_log import log_cap_hit
from ..server.rate_window import ByteRateFloor
from ..server.recipient import AbstractReader
from ..protocol.framing import _CRLF, is_informational
from ..protocol.http1 import (CHUNKED, CLOSE_DELIMITED, LENGTH, NO_BODY,
                              ChunkedDecoder, FieldsTooLarge, IncompleteReadError,
                              LineCache, MessageError, ReadLimitExceeded,
                              connection_options, content_length, parse_head,
                              persistent, request_framing, response_framing)
from ..server.sender import AbstractWriter, AsyncioWriter
from ._connect import (DEFAULT_CONNECT_TIMEOUT, ClientConnection,
                       open_connection as _open_connection)
from .exceptions import ConnectionError, ProtocolError, ResponseTooLarge
from .http2 import ClientResponse  # shared dataclass
from blackbull.fault_injection._scenario import _ClientDriver, half_close
from blackbull.fault_injection.scenario_h1 import (
    Scenario,
    ScenarioResult,
    SendRawBytes,
    response_matches,
)

logger = logging.getLogger(__name__)


# Type for request bodies — either a complete byte string or an async iterable
# of byte chunks (the streaming case, chunk-framed unless the caller declared a
# length for it).
RequestBody = Union[bytes, bytearray, memoryview, AsyncIterable[bytes]]


class _PreparedRequest(NamedTuple):
    """What [`prepare`][] settled: the head, the body, and the one length
    that bounds it."""

    head: bytes
    body: bytes | AsyncIterable[bytes]
    #: Total the body must reach, or ``None`` when it is chunk-framed and so
    #: announces no total.
    declared: int | None


# CRLF as used throughout RFC 7230.
#: Slice size for streaming a ``Content-Length`` body.  Matches the
#: server's own body-chunk default so both directions hand the same
#: shape to a caller.
_STREAM_CHUNK_SIZE: int = 64 * 1024


def _declared_content_length(headers: Headers) -> int | None:
    """The body length a request's caller fields declare, by the server's
    request rules: one framing, and ``Transfer-Encoding`` only as ``chunked``,
    the one coding this client writes."""
    try:
        declared, _chunked = request_framing(headers)
    except MessageError as exc:
        raise ProtocolError(str(exc)) from exc
    return declared


def _response_length(headers: Headers) -> int | None:
    try:
        return content_length(headers._index)
    except MessageError as exc:
        raise ProtocolError(str(exc)) from exc


# Methods for which an empty body still goes out as ``Content-Length: 0``:
# some origins and intermediaries answer 411 when a body-bearing method
# carries no length at all.
_BODY_ALLOWED_METHODS = frozenset({b'POST', b'PUT', b'PATCH', b'DELETE'})


class HTTP1RequestSender:
    """Writes an HTTP/1.1 request — request line, headers, body — to an ``AbstractWriter``.

    Owns the request's framing: a byte body is sent under one
    ``Content-Length``, an ``AsyncIterable`` body under that length when the
    caller declared one and under ``Transfer-Encoding: chunked`` when it did
    not.  The ``Host`` header MUST be present (RFC 7230 §5.4) — the helper
    raises ``ProtocolError`` if it is not.
    """

    def __init__(self, writer: AbstractWriter) -> None:
        self._writer = writer

    async def send(self, method: str | HTTPMethod, path: str,
                   headers: Headers, body: RequestBody = b'') -> None:
        await self.send_prepared(self.prepare(method, path, headers, body))

    @classmethod
    def prepare(cls, method: str | HTTPMethod, path: str,
                headers: Headers, body: RequestBody = b'') -> _PreparedRequest:
        """Validate and render everything that can fail before wire I/O.

        The high-level client claims response ownership only after this method
        succeeds.  A caller error such as a mismatched Content-Length or an
        unencodable request target therefore cannot retire an untouched
        keep-alive connection.

        Framing belongs to the sender, so the whole field pair is decided
        here: a caller's ``Content-Length`` is checked against the body and
        then written once, canonically.  A caller's ``Transfer-Encoding`` is
        honoured only where rewriting it cannot change what the body means —
        the value ``chunked``, the one framing this client writes — and
        refused otherwise.
        A body whose length is known goes out as that many raw octets, a
        stream included, with the total checked as it goes; one whose length
        is not knowable up front is chunk-framed instead.

        The return value is internal: pass it to [`send_prepared`][] unchanged.
        """
        if b'host' not in headers:
            raise ProtocolError('HTTP/1.1 request requires a Host header')

        method_text = str(method)
        declared = _declared_content_length(headers)
        pairs = [(name, value) for name, value in headers
                 if name.lower() not in (b'content-length', b'transfer-encoding')]

        if isinstance(body, (bytes, bytearray, memoryview)):
            fixed_body = bytes(body)
            expected = len(fixed_body)
            if declared is not None and declared != expected:
                raise ProtocolError(
                    f'caller-supplied Content-Length ({declared}) does not '
                    f'match body length ({expected} bytes); rejecting to '
                    f'avoid CL.CL smuggling on the wire')
            if (declared is not None or expected > 0
                    or method_text.upper().encode() in _BODY_ALLOWED_METHODS):
                pairs.append((b'content-length', str(expected).encode()))
            return _PreparedRequest(
                cls._build_start(method_text, path, pairs), fixed_body, expected)

        if declared is None:
            pairs.append((b'transfer-encoding', b'chunked'))
            return _PreparedRequest(
                cls._build_start(method_text, path, pairs), body, None)
        pairs.append((b'content-length', str(declared).encode()))
        return _PreparedRequest(
            cls._build_start(method_text, path, pairs), body, declared)

    async def send_prepared(self, prepared: _PreparedRequest) -> None:
        """Write a request returned by [`prepare`][]."""
        head, body, declared = prepared
        await self._writer.write(head)
        if isinstance(body, bytes):
            if body:
                await self._writer.write(body)
            return

        if declared is None:
            async for chunk in body:
                if chunk:
                    await self._writer.write(
                        f'{len(chunk):x}'.encode() + _CRLF + chunk + _CRLF)
            await self._writer.write(b'0' + _CRLF + _CRLF)
            return

        # A declared-length stream: raw octets, held to the total the head
        # announced.  Crossing it is caught before the octets go out; falling
        # short is caught once the body is spent.  Either way the request on
        # the wire no longer matches its head, so the caller must not reuse
        # this connection.
        sent = 0
        async for chunk in body:
            if not chunk:
                continue
            if sent + len(chunk) > declared:
                raise ProtocolError('request body exceeds Content-Length')
            sent += len(chunk)
            await self._writer.write(chunk)
        if sent != declared:
            raise ProtocolError('request body is shorter than Content-Length')

    @staticmethod
    def _build_start(method: str, path: str, headers: HeaderList) -> bytes:
        chunks: list[bytes] = [f'{method} {path} HTTP/1.1'.encode() + _CRLF]
        for k, v in headers:
            chunks.append(k + b': ' + v + _CRLF)
        chunks.append(_CRLF)
        return b''.join(chunks)


class Fate(enum.Enum):
    """What a finished response leaves its connection fit for."""

    #: Self-delimited and persistent: the next request may follow.
    REUSE = 'reuse'
    #: Valid, but the peer or the request closes the connection after it.
    RETIRE = 'retire'
    #: Delimited by the close (RFC 9112 §6.3 item 8): no second message.
    EXHAUSTED = 'exhausted'
    #: A 101 or a 2xx to CONNECT on a persistent connection: hand it off.
    SWITCH = 'switch'
    #: The read stopped part-way; the position in the stream is unknown.
    BROKEN = 'broken'


class HTTP1ResponseHeadReader:
    """Read bounded response heads without body or connection-reuse policy."""

    _boundary_declared = False

    @staticmethod
    def _connection_options(headers: Headers) -> list[bytes]:
        try:
            return connection_options(headers)
        except MessageError as exc:
            raise ProtocolError(str(exc)) from None

    @staticmethod
    async def _read_head(reader: AbstractReader) -> bytes:
        """Read a response head under the total budget, then enforce the per-line budget.
        """
        cfg = get_settings()
        timeout = cfg.client_head_timeout
        try:
            if timeout > 0:
                deadline = asyncio.timeout(timeout)
                try:
                    async with deadline:
                        head = await reader.read_head(cfg.client_head_max_total)
                except TimeoutError:
                    # Only an expired deadline is this cap refusing.  A
                    # TimeoutError raised from inside the reader for its own
                    # reasons belongs to whatever raised it, and naming the
                    # cap for it would be a record of something that did not
                    # happen.
                    if deadline.expired():
                        log_cap_hit('client_head_timeout', requested=timeout,
                                    limit=timeout, protocol='http1')
                    raise
            else:
                head = await reader.read_head(cfg.client_head_max_total)
        except ReadLimitExceeded as exc:
            log_cap_hit('client_head_max_total', requested=len(exc.seen),
                        limit=cfg.client_head_max_total, protocol='http1')
            raise ResponseTooLarge(
                f'response head exceeds '
                f'BB_CLIENT_HEAD_MAX_TOTAL={cfg.client_head_max_total}',
                exc.seen) from None
        except IncompleteReadError as exc:
            raise ConnectionError('connection closed before response') from exc
        if not head:
            raise ConnectionError('connection closed before response')

        return head

    async def receive_head(
            self, reader: AbstractReader, *, skip_interim: bool = True,
    ) -> tuple[bytes, int, Headers]:
        """Read through bounded interim heads and return the final head.

        RFC 9110 requires clients to parse one or more 1xx responses before a
        final response.  101 is returned immediately because the following
        bytes belong to the switched protocol.  The count cap owns aggregate
        work while the existing head limits continue to own each individual
        head.
        """
        limit = get_settings().client_max_interim_responses
        seen = 0
        while True:
            version, status, headers = await self._read_message_head(reader)
            if (not skip_interim or status == 101
                    or not is_informational(status)):
                return version, status, headers
            # Content-Length: 0 claims no boundary. What follows cannot be
            # told apart from the final response being answered.
            if headers.getlist(b'transfer-encoding') or _response_length(headers):
                self._boundary_declared = True
            seen += 1
            if limit and seen > limit:
                log_cap_hit('client_max_interim_responses', requested=seen,
                            limit=limit, protocol='http1')
                raise ResponseTooLarge(
                    f'peer sent more than '
                    f'BB_CLIENT_MAX_INTERIM_RESPONSES={limit} interim '
                    f'responses without a final one')

    async def _read_message_head(
            self, reader: AbstractReader) -> tuple[bytes, int, Headers]:
        head = await self._read_head(reader)
        max_line = get_settings().client_head_max_line
        try:
            (version, status), raw, index = parse_head(
                head, max_line, LineCache(), False)
        except FieldsTooLarge as exc:
            log_cap_hit('client_head_max_line', requested=exc.length,
                        limit=max_line, protocol='http1')
            raise ResponseTooLarge(
                f'response header line {exc.length} bytes > '
                f'BB_CLIENT_HEAD_MAX_LINE={max_line}', head[:max_line]) from None
        except MessageError as exc:
            raise ProtocolError(str(exc)) from None
        return version, status, Headers._adopt(raw, index)


class HTTP1ResponseRecipient(HTTP1ResponseHeadReader):
    """Reads an HTTP/1.1 response from an ``AbstractReader``.

    Decodes ``Content-Length``-bound, ``Transfer-Encoding: chunked``, and
    connection-close-delimited bodies.  Returns a ``ClientResponse``;
    ``stream()`` returns an async iterator of body chunks instead, so large
    responses don't have to fit in memory — true of all body framings.
    """

    def __init__(self, request_method: str | bytes | HTTPMethod | None = None) -> None:
        #: The method that caused this response, when known.  Direct users of
        #: the low-level recipient may omit it for compatibility; the public
        #: HTTP1Client paths always provide it.
        self.request_method = request_method
        #: What the last response leaves the connection fit for, or ``None``
        #: before one is read.
        self.fate: Fate | None = None
        #: An interim response declared a body it cannot have.
        self._boundary_declared = False
        #: Parsed response protocol version, retained for persistence policy.
        self.http_version: bytes | None = None
        #: The high-level sender sets this when its request included
        #: ``Connection: close``.
        self.request_close = False
        #: Built on the first body read from the settings then in force and
        #: kept for the message: a window that did not span reads could not
        #: measure a rate.  One recipient per response, so it needs no reset.
        self._rate_floor: ByteRateFloor | None = None
        #: Set by the first read that delivered a body octet.  Until then the
        #: peer is thinking, not dripping, and the floor is not watching.
        self._body_open = False
        #: A short window seen on a read that delivered no payload.  Not yet a
        #: verdict — see [`_body_read`][].
        self._unpaid_framing = False

    def _refuse_if_broken(self) -> None:
        if self.fate is Fate.BROKEN:
            raise ConnectionError(
                'this reader was abandoned part-way through a message — '
                'its position in the byte stream is unknown')
        if self.fate is not None and self.fate is not Fate.REUSE:
            raise ConnectionError(
                'this reader is not reusable after a non-reusable response')

    async def receive(
            self, reader: AbstractReader, *,
            method: str | bytes | HTTPMethod | None = None,
            skip_interim: bool = True,
    ) -> ClientResponse:
        """Read one final response, optionally exposing an interim response.

        Production callers keep ``skip_interim=True`` and therefore pass over
        100/102/103 responses until the final response.  ``101`` is final by
        protocol-switch semantics.  The low-level fault-injection API passes
        ``skip_interim=False`` so each peer message remains observable.

        ``method=`` overrides the constructor's, for a caller that knows it
        only at the read.
        """
        self._refuse_if_broken()
        try:
            status, headers, framing = await self._read_head_and_policy(
                reader, skip_interim=skip_interim, method=method)
            body = await self._read_body(reader, framing)
        except BaseException:
            # Every refusal below this point leaves unread octets behind, and
            # a peer whose body is itself a well-formed response gets one
            # delivered for a request the server answered differently.  Which
            # error it was does not matter — what matters is that we stopped
            # somewhere the peer chose.
            self.fate = Fate.BROKEN
            raise
        return ClientResponse(status=status, headers=headers, body=body)

    async def stream(
            self, reader: AbstractReader, *,
            method: str | bytes | HTTPMethod | None = None,
    ) -> AsyncIterator[bytes]:
        # Body-only streaming: callers that need status/headers should use
        # ``receive``.  Yielding the start-line as the first item would force
        # callers to special-case the iterator's first element.  The status is
        # not returned but is still needed, because it is half of what decides
        # whether there is a body to yield at all.
        self._refuse_if_broken()
        try:
            _status, _headers, framing = await self._read_head_and_policy(
                reader, method=method)
            async for chunk in self._stream_body(reader, framing):
                yield chunk
        except BaseException:
            self.fate = Fate.BROKEN
            raise

    @staticmethod
    def _response_is_persistent(version: bytes, headers: Headers) -> bool:
        try:
            return persistent(version, headers)
        except MessageError as exc:
            raise ProtocolError(str(exc)) from None

    async def _body_read(self, coro, *, payload: bool = False,
                         allow_eof: bool = False):
        """Bound one body read by its timeout and response-body rate floor.

        Only payload octets count toward the rate; transport wait for framing also
        counts. Caller time between stream() yields must not count against the peer.
        Normalize short EOF and reader truncation errors to the client exception.
        """
        cfg = get_settings()
        timeout = cfg.client_body_timeout
        floor = self._rate_floor
        if floor is None:
            floor = self._rate_floor = ByteRateFloor(
                cfg.client_min_body_rate, cfg.client_min_body_rate_grace)
        # Read the flag before the read, not after: the wait that *delivers*
        # the first octet is the peer's think time, which is outside the
        # window.  A peer that flushes its head and then works — a slow query,
        # a report built while it is streamed — is not dripping.
        watching = self._body_open
        started = _monotonic() if watching else 0.0
        try:
            if timeout > 0:
                deadline = asyncio.timeout(timeout)
                try:
                    async with deadline:
                        data = await coro
                except TimeoutError:
                    # The deadline that expired is the one that refused.  A
                    # TimeoutError out of the reader itself is somebody
                    # else's, and the rate floor below raises one of its own
                    # after this block, under its own cap's name.
                    if deadline.expired():
                        log_cap_hit('client_body_timeout', requested=timeout,
                                    limit=timeout, protocol='http1')
                    raise
            else:
                data = await coro
        except IncompleteReadError as exc:
            raise ConnectionError('connection closed mid-body') from exc
        if allow_eof and not data:
            # EOF is the delimiter for this framing mode, but after the first
            # body octet the wait for that delimiter is still peer wait.  The
            # first body read remains exempt; only an open floor is settled
            # here, so close-delimited completion cannot bypass it.
            if watching:
                short = floor.record(0, _monotonic() - started)
                if short:
                    log_cap_hit('client_min_body_rate', requested=floor.observed,
                                limit=floor.rate, protocol='http1')
                    raise TimeoutError(
                        f'response body arriving below '
                        f'BB_CLIENT_MIN_BODY_RATE={floor.rate} B/s')
                self._unpaid_framing = short
            return data
        if payload and data:
            self._body_open = True
        if watching:
            short = floor.record(len(data) if payload else 0,
                                 _monotonic() - started)
            # Defer a short framing read judgement until the next read can deliver payload.
            # Keep its waiting time in the rate denominator.
            if short and (payload or self._unpaid_framing):
                log_cap_hit('client_min_body_rate', requested=floor.observed,
                            limit=floor.rate, protocol='http1')
                raise TimeoutError(
                    f'response body arriving below '
                    f'BB_CLIENT_MIN_BODY_RATE={floor.rate} B/s')
            self._unpaid_framing = short
        return data

    @staticmethod
    def _body_framing(status: int, headers: Headers,
                      request_method: str | bytes | HTTPMethod | None
                      ) -> tuple[str, int | None, bool, bool]:
        """``(mode, declared_length, reusable, switched)`` by the codec's response rules."""
        try:
            return response_framing(headers, request_method, status)
        except MessageError as exc:
            raise ProtocolError(str(exc)) from exc

    def _record_framing(self, framing: tuple[str, int | None, bool, bool], *,
                        response_persistent: bool) -> None:
        """The one place the connection's fate is decided.

        Framing says whether this message delimits itself; ``Connection`` says
        whether the peer means to keep the connection.  Reuse needs both.
        """
        mode, _declared, framing_reusable, switched = framing
        # A peer that named a boundary it cannot have is not switching anything.
        if switched and not self._boundary_declared:
            self.fate = (Fate.SWITCH if response_persistent and not self.request_close
                         else Fate.RETIRE)
        elif mode == CLOSE_DELIMITED:
            self.fate = Fate.EXHAUSTED
        elif (framing_reusable and response_persistent and not self.request_close
              and not self._boundary_declared):
            self.fate = Fate.REUSE
        else:
            self.fate = Fate.RETIRE

    async def _read_head_and_policy(
            self, reader: AbstractReader, *, skip_interim: bool = True,
            method: str | bytes | HTTPMethod | None = None,
    ) -> tuple[int, Headers, tuple[str, int | None, bool, bool]]:
        """Read the head and settle everything the body read must not decide."""
        version, status, headers = await self.receive_head(
            reader, skip_interim=skip_interim)
        self.http_version = version
        request_method = (method if method is not None
                          else self.request_method)
        framing = self._body_framing(status, headers, request_method)
        self._record_framing(framing, response_persistent=self._response_is_persistent(
            version, headers))
        return status, headers, framing

    async def _read_body(self, reader: AbstractReader,
                         framing: tuple[str, int | None, bool, bool]) -> bytes:
        # The total lives here and not in ``_read_chunked``, which
        # ``_stream_body`` shares: this is the path that accumulates, and the
        # other one exists precisely so a large response need not.
        max_total = get_settings().client_body_max_total
        mode, declared, _reusable, _switched = framing
        if mode == NO_BODY:
            return b''
        if mode == CHUNKED:
            return b''.join([c async for c in
                             self._read_chunked(reader, max_total=max_total)])
        if mode == LENGTH:
            assert declared is not None
            if max_total and declared > max_total:
                # Refused on the declaration, before an octet is read — the
                # peer already told us it will not fit.
                log_cap_hit('client_body_max_total', requested=declared,
                            limit=max_total, protocol='http1')
                raise ResponseTooLarge(
                    f'declared body {declared} bytes exceeds '
                    f'BB_CLIENT_BODY_MAX_TOTAL={max_total}')
            # In slices, like the streaming path: one ``readexactly`` for the
            # whole body is a single read to every bound above it.
            return b''.join([chunk async for chunk in
                             self._read_declared(reader, declared)])
        return b''.join([chunk async for chunk in
                         self._read_close_delimited(reader, max_total)])

    async def _stream_body(self, reader: AbstractReader,
                           framing: tuple[str, int | None, bool, bool]
                           ) -> AsyncIterator[bytes]:
        mode, declared, _reusable, _switched = framing
        if mode == NO_BODY:
            return
        if mode == CHUNKED:
            async for chunk in self._read_chunked(reader):
                yield chunk
            return
        if mode == LENGTH:
            assert declared is not None
            async for chunk in self._read_declared(reader, declared):
                yield chunk
            return
        async for chunk in self._read_close_delimited(reader):
            yield chunk

    async def _read_close_delimited(
            self, reader: AbstractReader, max_total: int = 0
    ) -> AsyncIterator[bytes]:
        """Read an allowed response body through connection EOF.

        The buffered path passes its total budget here.  Once the budget has
        been reached, one additional byte is read only to prove that the
        peer exceeded it; it is never appended to the accumulated body.
        """
        seen = 0
        while True:
            read_size = _STREAM_CHUNK_SIZE
            if max_total:
                read_size = min(read_size, max_total - seen)
                if read_size <= 0:
                    read_size = 1
            chunk = await self._body_read(
                reader.read(read_size), payload=True, allow_eof=True)
            if not chunk:
                return
            if max_total and seen + len(chunk) > max_total:
                log_cap_hit('client_body_max_total',
                            requested=seen + len(chunk),
                            limit=max_total, protocol='http1')
                raise ResponseTooLarge(
                    f'body exceeds BB_CLIENT_BODY_MAX_TOTAL={max_total}')
            seen += len(chunk)
            yield chunk

    async def _read_declared(self, reader: AbstractReader,
                             declared: int) -> AsyncIterator[bytes]:
        """Yield a Content-Length body in bounded, transport-paced slices.

        Use up-to-n reads: readexactly would hide transport progress and turn the
        per-read timeout into a deadline for a whole slice.
        """
        remaining = declared
        while remaining > 0:
            chunk = await self._body_read(
                reader.read(min(remaining, _STREAM_CHUNK_SIZE)), payload=True)
            if not chunk:
                # Reject a short EOF instead of looping without progress.
                raise ConnectionError('connection closed mid-body')
            remaining -= len(chunk)
            yield chunk

    async def _wait(self, read, payload: bool):
        return await self._body_read(read, payload=payload)

    async def _read_chunked(self, reader: AbstractReader, *,
                            max_total: int = 0) -> AsyncIterator[bytes]:
        cfg = get_settings()
        decoder = ChunkedDecoder(
            reader, self._wait, line_max=cfg.client_head_max_line,
            slice_max=_STREAM_CHUNK_SIZE, trailer_max=cfg.client_head_max_total)
        seen = 0
        while True:
            try:
                piece = await decoder.next()
            except FieldsTooLarge as exc:
                cap = 'client_head_max_total' if exc.section else 'client_head_max_line'
                log_cap_hit(cap, requested=exc.length, limit=exc.limit,
                            protocol='http1')
                raise ResponseTooLarge(
                    f'{exc} (BB_{cap.upper()}={exc.limit})') from None
            except MessageError as exc:
                raise ProtocolError(str(exc)) from None
            except EOFError as exc:
                raise ConnectionError('connection closed mid-chunk') from exc
            if piece is None:
                return
            seen += len(piece)
            if max_total and seen > max_total:
                log_cap_hit('client_body_max_total', requested=seen,
                            limit=max_total, protocol='http1')
                raise ResponseTooLarge(
                    f'body exceeds BB_CLIENT_BODY_MAX_TOTAL={max_total}')
            yield piece


def _record_response(result, response) -> None:
    """Append to received and set response to the newest response.
    """
    result.response = response
    result.received.append(response)
    body = getattr(response, 'body', b'') or b''
    result.server_bytes_received += len(body)


class HTTP1UpgradeSession:
    """Bidirectional transport after CONNECT or an HTTP 101 switch.

    Obtain one with [`HTTP1Client.handoff`][HTTP1Client.handoff].  The handoff is one-shot:
    once returned, this session owns transport closure and the originating
    HTTP client can no longer read or write the connection.
    """

    def __init__(self, reader: AbstractReader, writer: AbstractWriter,
                 raw_writer: ClientConnection) -> None:
        self._reader = reader
        self._writer = writer
        self._raw_writer = raw_writer
        self._closed = False

    async def read(self, n: int = -1) -> bytes:
        """Read switched-protocol bytes, including HTTP read-ahead bytes."""
        if self._closed:
            raise ConnectionError('upgrade session is closed')
        return await self._reader.read(n)

    async def write(self, data: bytes) -> None:
        """Write raw switched-protocol bytes."""
        if self._closed:
            raise ConnectionError('upgrade session is closed')
        await self._writer.write(data)

    async def close(self) -> None:
        """Close the transferred transport.  Idempotent."""
        if self._closed:
            return
        self._closed = True
        self._raw_writer.close()
        await self._raw_writer.wait_closed()

    async def __aenter__(self) -> 'HTTP1UpgradeSession':
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.close()


class _State(enum.Enum):
    """Where an ``HTTP1Client`` connection is in its life."""

    READY = 'ready'
    #: Abandoned mid-message: the position in the byte stream is unknown.
    BROKEN = 'broken'
    #: A close-delimited response consumed the connection.
    EXHAUSTED = 'exhausted'
    #: A valid response the connection cannot follow with another request.
    RETIRED = 'retired'
    #: A completed CONNECT or 101; [`HTTP1Client.handoff`][] is available.
    SWITCHED = 'switched'
    HANDED_OFF = 'handed off'
    CLOSED = 'closed'


class HTTP1Client:
    """Async HTTP/1.1 client.

    Use as an async context manager::

        async with HTTP1Client('localhost', 8000) as c:
            res = await c.request(HTTPMethod.GET, '/path')

    The connection persists across multiple ``request()`` calls when HTTP
    version, Connection options, and body framing permit it.  A successful
    CONNECT or 101 can be transferred with [`handoff`][]; after that, the
    returned [`HTTP1UpgradeSession`][] owns transport closure.  Pass
    ``ssl=`` to use TLS.

    The ``Host`` header is injected automatically when the caller omits it.
    """

    def __init__(self, host: str, port: int, *,
                 ssl: _ssl.SSLContext | None = None,
                 record_wire_bytes: bool = False,
                 connect_timeout: float | None = DEFAULT_CONNECT_TIMEOUT) -> None:
        self._host = host
        self._port = port
        self._ssl = ssl
        self._reader: AbstractReader | None = None
        self._writer: AbstractWriter | None = None
        self._raw_writer: ClientConnection | None = None
        # Opt-in wire-bytes capture for the low-level
        # primitives (send_raw, send_request_line, send_header_line, ...).
        # When True, every byte sent through those methods is also appended
        # to ``self._wire_buffer`` for later inspection by failing tests.
        # request() does NOT populate this buffer; only the low-level
        # primitives record.
        self._record_wire_bytes = record_wire_bytes
        self._wire_buffer: bytearray = bytearray()
        # Bound the connect() so a hung accept can't stall the scenario
        # executor before any step runs.  atheris in particular cannot afford
        # an unbounded wait per TestOneInput.  None opts out, leaving the
        # caller to impose their own deadline.
        self._connect_timeout = connect_timeout
        self._state = _State.READY
        #: The response whose body owns the reader, until its delimiter is read.
        self._active_response: HTTP1ResponseRecipient | None = None

    # ---- async context manager -------------------------------------------

    async def __aenter__(self) -> 'HTTP1Client':
        if self._raw_writer is None:
            conn = await _open_connection(self._host, self._port, self._ssl,
                                          self._connect_timeout)
            self._raw_writer = conn
            self._reader = conn.reader
            self._writer = AsyncioWriter(
                conn, get_settings().client_write_timeout,
                cap_name='client_write_timeout', protocol='http1')
        await self._start()
        return self

    @classmethod
    def _adopt(cls, host: str, port: int,
               conn: ClientConnection,
               *, ssl: _ssl.SSLContext | None = None) -> 'HTTP1Client':
        """Wrap an already-open connection as an HTTP1Client.

        HTTP/1.1 owes nothing on connect, so the client is usable on return.
        """
        c = cls(host, port, ssl=ssl)
        c._raw_writer = conn
        c._reader = conn.reader
        c._writer = AsyncioWriter(
            conn, get_settings().client_write_timeout,
            cap_name='client_write_timeout', protocol='http1')
        return c

    async def _start(self) -> None:
        """Per-protocol post-connect initialisation hook.

        HTTP/1.1 has no preface or persistent loop, so this is a no-op; it
        exists for API symmetry with ``HTTP2Client._start``.
        """
        return None

    async def __aexit__(self, exc_type, exc, tb) -> None:
        # Irreversible even if the close is best-effort or the peer closed first.
        if self._state is not _State.HANDED_OFF:
            self._state = _State.CLOSED
        if self._raw_writer is not None:
            try:
                self._raw_writer.close()
                await self._raw_writer.wait_closed()
            except Exception:
                pass  # best-effort close on context exit; peer may already be gone.

    # ---- public API ------------------------------------------------------

    def _end(self, state: '_State') -> None:
        """Leave READY for *state*, closing the transport unless it is to be
        handed off.  CLOSED and HANDED_OFF are final."""
        if self._state is _State.READY:
            self._state = state
        if self._state is not _State.SWITCHED and self._raw_writer is not None:
            try:
                self._raw_writer.close()
            except Exception:
                pass  # best-effort: the peer may already be gone.

    def _begin_response(self, recipient: HTTP1ResponseRecipient) -> None:
        if self._active_response is not None:
            raise ConnectionError(
                'a response body is still being consumed on this connection')
        self._refuse_if_desynced()
        self._active_response = recipient

    def _finish_response(self, recipient: HTTP1ResponseRecipient, *,
                         complete: bool,
                         abandon_incomplete: bool = True) -> None:
        """Release response ownership and apply its terminal state."""
        if self._active_response is recipient:
            self._active_response = None
        fate = recipient.fate
        if fate is Fate.BROKEN or not complete:
            # Closing between yields has not proved the body delimiter was
            # consumed.  The fault-injection read_response keeps inspecting.
            if abandon_incomplete:
                self._end(_State.BROKEN)
        elif fate is Fate.SWITCH:
            self._end(_State.SWITCHED)
        elif fate is Fate.EXHAUSTED:
            self._end(_State.EXHAUSTED)
        elif fate is Fate.RETIRE:
            self._end(_State.RETIRED)

    def _refuse_if_desynced(self) -> None:
        state = self._state
        if state is _State.CLOSED:
            raise ConnectionError('HTTP1Client context is closed')
        if self._active_response is not None:
            raise ConnectionError(
                'a response body is still being consumed on this connection')
        if state is _State.BROKEN:
            raise ConnectionError(
                'connection abandoned after a framing error — '
                'its position in the byte stream is unknown')
        if state is _State.EXHAUSTED:
            raise ConnectionError(
                'the last response was delimited by the connection close — '
                'this connection cannot carry another request')
        if state is not _State.READY:
            raise ConnectionError(
                'connection is not reusable after a non-reusable response')

    async def request(self, method: str | HTTPMethod, path: str, *,
                      headers: HeaderList = (),
                      body: RequestBody = b'') -> ClientResponse:
        self._refuse_if_desynced()
        assert self._writer is not None and self._reader is not None
        h = self._headers_with_host(headers)
        sender = HTTP1RequestSender(self._writer)
        prepared = sender.prepare(method, path, h, body)
        request_close = (
            b'close' in HTTP1ResponseRecipient._connection_options(h))
        recipient = HTTP1ResponseRecipient(request_method=method)
        recipient.request_close = request_close
        self._begin_response(recipient)
        complete = False
        try:
            await sender.send_prepared(prepared)
            response = await recipient.receive(self._reader, method=method)
            complete = True
            return response
        finally:
            self._finish_response(recipient, complete=complete)

    async def stream(self, method: str | HTTPMethod, path: str, *,
                     headers: HeaderList = (),
                     body: RequestBody = b'') -> AsyncIterator[bytes]:
        """Send a request and yield body chunks lazily.

        Unlike ``request()`` this does not buffer the response body, so
        gigabyte-sized responses do not need to fit in memory.  Status and
        headers are not exposed by this method; use ``request()`` if you
        need them.
        """
        self._refuse_if_desynced()
        assert self._writer is not None and self._reader is not None
        h = self._headers_with_host(headers)
        sender = HTTP1RequestSender(self._writer)
        prepared = sender.prepare(method, path, h, body)
        request_close = (
            b'close' in HTTP1ResponseRecipient._connection_options(h))
        recipient = HTTP1ResponseRecipient(request_method=method)
        recipient.request_close = request_close
        self._begin_response(recipient)
        complete = False
        try:
            await sender.send_prepared(prepared)
            async for chunk in recipient.stream(self._reader, method=method):
                yield chunk
            complete = True
        finally:
            self._finish_response(recipient, complete=complete)

    def _headers_with_host(self, headers: HeaderList) -> Headers:
        h = Headers(list(headers))
        if b'host' not in h:
            h.append(b'host', f'{self._host}:{self._port}'.encode())
        return h

    def handoff(self) -> HTTP1UpgradeSession:
        """Transfer a completed CONNECT/101 transport to a raw session.

        The returned session preserves bytes already buffered by the HTTP
        reader and exposes both read and write sides.  This operation is
        available exactly once.  If the client context exits before handoff,
        the client closes the transport; after handoff, the session owns it.
        """
        if self._state is _State.CLOSED:
            raise ConnectionError('HTTP1Client context is closed')
        if self._state is _State.HANDED_OFF:
            raise ConnectionError('the switched transport was already handed off')
        if self._state is not _State.SWITCHED:
            raise ConnectionError(
                'no completed CONNECT or 101 response is available for handoff')
        assert self._reader is not None and self._writer is not None
        assert self._raw_writer is not None
        session = HTTP1UpgradeSession(
            self._reader, self._writer, self._raw_writer)
        self._state = _State.HANDED_OFF
        # The session now owns close.  In particular HTTP1Client.__aexit__
        # must not close a transport that has escaped its context.
        self._raw_writer = None
        return session

    # ---- Low-level test-instrument primitives ----------------------------
    #
    # These bypass HTTP1RequestSender's safety net: no Host injection, no
    # Content-Length, no validation.  What they are for is
    # ``docs/guide/client.md`` §Driving a misbehaving peer.

    @property
    def wire_buffer(self) -> bytes:
        """Bytes sent so far by the low-level primitives in this session.

        Empty unless the client was constructed with
        ``record_wire_bytes=True``.  Reset with [`reset_wire_buffer`][].
        """
        return bytes(self._wire_buffer)

    def reset_wire_buffer(self) -> None:
        """Discard previously captured wire bytes."""
        self._wire_buffer.clear()

    async def send_raw(self, data: bytes, *,
                       byte_interval: float = 0.0) -> None:
        """Push arbitrary bytes onto the underlying socket.

        With ``byte_interval > 0`` the bytes go out one at a time, that many
        seconds apart — the primitive a slowloris-style stall is built from.
        Each write is drained, so the pacing is on the wire and not in the
        asyncio send buffer.
        """
        if self._state is _State.HANDED_OFF:
            raise ConnectionError('transport ownership was handed off')
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
        """Send body bytes with the same semantics as send_raw.
        """
        await self.send_raw(data, byte_interval=byte_interval)

    async def send_chunk(self, data: bytes) -> None:
        """Send one ``Transfer-Encoding: chunked`` chunk.

        Caller must have already emitted ``Transfer-Encoding: chunked``
        via [`send_header_line`][] and called [`end_headers`][].
        Finish the chunked stream with [`end_chunked`][]."""
        await self.send_raw(f'{len(data):x}'.encode() + _CRLF + data + _CRLF)

    async def end_chunked(self) -> None:
        """Emit the size-0 terminator chunk that closes a chunked body."""
        await self.send_raw(b'0' + _CRLF + _CRLF)

    async def read_response(
            self, *,
            request_method: str | bytes | HTTPMethod | None = None,
            timeout: float | None = None) -> ClientResponse:
        """Read one HTTP/1.1 response from the connection.

        Optional ``timeout`` bounds the entire read (status line + headers
        + body).  Raises ``asyncio.TimeoutError`` if the deadline is
        hit; the caller decides whether to treat that as a transport-
        fail or a normal protocol outcome."""
        if self._state is _State.CLOSED:
            raise ConnectionError('HTTP1Client context is closed')
        if self._state is not _State.READY:
            raise ConnectionError(
                'connection is not reusable after a non-reusable response')
        if self._active_response is not None:
            raise ConnectionError(
                'a response body is still being consumed on this connection')
        assert self._reader is not None, 'connect via __aenter__ first'
        recipient = HTTP1ResponseRecipient(request_method=request_method)
        self._active_response = recipient
        complete = False
        try:
            # The fault-injection primitive observes each peer response;
            # unlike high-level request()/stream(), it must surface 1xx.
            coro = recipient.receive(
                self._reader, method=request_method, skip_interim=False)
            if timeout is None:
                response = await coro
            else:
                response = await asyncio.wait_for(coro, timeout=timeout)
            complete = True
            return response
        finally:
            # Keep read_response()'s low-level fault-injection contract: a
            # failed read is returned to the caller without automatically
            # poisoning the client.  Successful terminal states still retire
            # HTTP or enter CONNECT tunnel mode.
            self._finish_response(
                recipient, complete=complete, abandon_incomplete=False)

    async def execute_scenario(
        self, scenario: Scenario,
    ) -> ScenarioResult:
        """Run connected-socket scenario steps and collect outcomes in ScenarioResult.

        Record peer responses, timeouts and transport failures; Abort stops
        further steps.  The vocabulary is ``docs/guide/fault_injection.md``.
        """
        assert self._writer is not None, 'connect via __aenter__ first'
        return await _H1ScenarioDriver(self, scenario).execute(scenario.steps)


class _H1ScenarioDriver(_ClientDriver):
    _matches = staticmethod(response_matches)

    def __init__(self, client: HTTP1Client, scenario: Scenario) -> None:
        super().__init__(scenario, ScenarioResult())
        self.client = client

    async def _write(self, data: bytes, byte_interval: float) -> None:
        await self.client.send_raw(data, byte_interval=byte_interval)

    async def _read(self, timeout: float):
        return await self.client.read_response(timeout=timeout)

    def _seen(self, response, *, kept: bool) -> None:
        _record_response(self.result, response)

    def _half_close(self) -> bool:
        return half_close(self.client._raw_writer)

    async def _close(self, *, graceful: bool) -> None:
        if self.client._raw_writer is not None:
            self.client._raw_writer.transport.abort()


# Unannotated: see tests/unit/test_deprecated_send_bytes_spellings.py::test_the_warning_is_attributed_to_the_callers_line.
def __getattr__(name):
    """Deprecated SendBytes alias for SendRawBytes; removal no earlier than 2027-08-19.
    """
    if name == 'SendBytes':
        import warnings  # noqa: PLC0415
        warnings.warn(
            f"{__name__}.SendBytes is deprecated; use SendRawBytes.  Removal no "
            "earlier than 2027-08-19.",
            DeprecationWarning, stacklevel=2)
        return SendRawBytes
    raise AttributeError(f'module {__name__!r} has no attribute {name!r}')
