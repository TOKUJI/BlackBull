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
from time import monotonic as _monotonic
from http import HTTPMethod
from typing import Union

import logging
from ..env import get_settings
from ..headers import Headers, HeaderList
from ..server.cap_log import log_cap_hit
from ..server.rate_window import ByteRateFloor
from ..server.recipient import (AbstractReader, AsyncioReader,
                                IncompleteReadError, ReadLimitExceeded,
                                _accepts_read_limit)
from ..server.sender import AbstractWriter, AsyncioWriter
from ._connect import DEFAULT_CONNECT_TIMEOUT, open_connection as _open_connection
from .exceptions import ConnectionError, ProtocolError, ResponseTooLarge
from .http2 import ClientResponse  # shared dataclass
from blackbull.fault_injection._transport import half_close as _sc_half_close
from blackbull.fault_injection.scenario_h1 import (
    Abort,
    ExpectResponse,
    HalfClose,
    ReadResponse,
    WaitForResponse,
    response_matches,
    Scenario,
    ScenarioResult,
    SendRawBytes as SendBytes,
    Sleep,
)

logger = logging.getLogger(__name__)


# Type for request bodies — either a complete byte string or an async iterable
# of byte chunks (the streaming case, encoded as Transfer-Encoding: chunked).
RequestBody = Union[bytes, bytearray, memoryview, AsyncIterable[bytes]]
_PreparedRequest = tuple[bytes, bytes | AsyncIterable[bytes], bool]

# CRLF as used throughout RFC 7230.
#: Slice size for streaming a ``Content-Length`` body.  Matches the
#: server's own body-chunk default so both directions hand the same
#: shape to a caller.
_STREAM_CHUNK_SIZE: int = 64 * 1024

_CRLF = b'\r\n'

#: How a response body ends — the outcome of RFC 9112 §6.3's ordered decision.
#: Named rather than inlined because two readers make the same decision and a
#: third (the ``no body at all`` case) precedes both.
_CHUNKED = 'chunked'
_DECLARED = 'declared'
_UNTIL_CLOSE = 'until-close'

#: RFC 9112 §7.1 — ``chunk-size = 1*HEXDIG``.  ``int(x, 16)`` is far laxer:
#: it takes a sign, an ``0x`` prefix, underscore separators and surrounding
#: whitespace, and a negative numeral reached ``readexactly()``.
_HEXDIG = frozenset(b'0123456789abcdefABCDEF')

# RFC 9110 §5.6.2.  Transfer-Encoding is an HTTP list, not a comma split:
# parameters may contain quoted commas and each coding is a token followed by
# zero or more ``; name=value`` parameters.
_TCHAR = frozenset(
    b"!#$%&'*+-.^_`|~"
    b'0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz')

#: RFC 9110 §5.5 field-value: HTAB, VCHAR and obs-text.  Deleting these from a
#: value leaves exactly the octets it may not carry, so the whole check is one
#: C-level ``translate`` rather than one Python step per octet.
_FIELD_VCHAR = (bytes([0x09]) + bytes(range(0x20, 0x7f))
                + bytes(range(0x80, 0x100)))
# Empty list members are tolerated for interoperability, but the parser must
# not spend unbounded work on a peer sending only commas.  The head-size budget
# remains the total byte bound; this is only a small structural sanity bound.
_MAX_EMPTY_TRANSFER_MEMBERS = 16


def _skip_ows(value: bytes, pos: int) -> int:
    while pos < len(value) and value[pos] in (0x20, 0x09):
        pos += 1
    return pos


def _te_token(value: bytes, pos: int) -> tuple[bytes, int]:
    start = pos
    while pos < len(value) and value[pos] in _TCHAR:
        pos += 1
    if pos == start:
        raise ProtocolError(
            f'invalid Transfer-Encoding token at position {pos}')
    return value[start:pos], pos


def _te_quoted_string(value: bytes, pos: int) -> int:
    """The opening quote is consumed here.  quoted-pair permits only
    HTAB/SP/VCHAR/obs-text after the backslash."""
    pos += 1
    while pos < len(value):
        octet = value[pos]
        if octet == 0x22:
            return pos + 1
        if octet == 0x5c:
            pos += 1
            if pos >= len(value) or not (
                    value[pos] == 0x09 or value[pos] == 0x20
                    or 0x21 <= value[pos] <= 0x7e
                    or value[pos] >= 0x80):
                raise ProtocolError(
                    'invalid quoted Transfer-Encoding parameter')
        elif (octet < 0x20 and octet != 0x09) or octet == 0x7f:
            raise ProtocolError(
                'invalid quoted Transfer-Encoding parameter')
        pos += 1
    raise ProtocolError('unterminated quoted Transfer-Encoding parameter')


# Methods for which an empty body still warrants an
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
        await self.send_prepared(self.prepare(method, path, headers, body))

    @classmethod
    def prepare(cls, method: str | HTTPMethod, path: str,
                headers: Headers, body: RequestBody = b'') -> _PreparedRequest:
        """Validate and render everything that can fail before wire I/O.

        The high-level client claims response ownership only after this method
        succeeds.  A caller error such as a mismatched Content-Length or an
        unencodable request target therefore cannot retire an untouched
        keep-alive connection.
        """
        if b'host' not in headers:
            raise ProtocolError('HTTP/1.1 request requires a Host header')

        method_text = str(method)
        if isinstance(body, (bytes, bytearray, memoryview)):
            fixed_body = bytes(body)
            cls._normalize_content_length(method_text, headers, fixed_body)
            return cls._build_start(method_text, path, headers), fixed_body, False

        if b'transfer-encoding' not in headers:
            headers.append(b'transfer-encoding', b'chunked')
        return cls._build_start(method_text, path, headers), body, True

    async def send_prepared(self, prepared: _PreparedRequest) -> None:
        """Write a request returned by :meth:`prepare`."""
        head, body, chunked = prepared
        await self._writer.write(head)
        if not chunked:
            assert isinstance(body, bytes)
            if body:
                await self._writer.write(body)
            return

        assert not isinstance(body, bytes)
        async for chunk in body:
            if chunk:
                await self._writer.write(
                    f'{len(chunk):x}'.encode() + _CRLF + chunk + _CRLF)
        await self._writer.write(b'0' + _CRLF + _CRLF)

    @staticmethod
    def _normalize_content_length(method: str, headers: Headers,
                                  body: bytes) -> None:
        """Emit / validate the ``Content-Length`` header for the request.

        Replaces the previous "add CL only if body is
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
        # Unknown method: forwards-compatible fallback — omit CL on an
        # empty body.
        return

    @staticmethod
    def _build_start(method: str, path: str, headers: Headers) -> bytes:
        chunks: list[bytes] = [f'{method} {path} HTTP/1.1'.encode() + _CRLF]
        for k, v in headers:
            chunks.append(k + b': ' + v + _CRLF)
        chunks.append(_CRLF)
        return b''.join(chunks)


class HTTP1ResponseRecipient:
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
        #: Set when a read failed part-way through a message, which makes the
        #: reader's position unknown.  The connection is then unusable: the
        #: rest of the message is still on the wire, so the next read would
        #: begin inside it and parse a body as a response.  The server's
        #: recipient carries the same flag for the same reason.
        self.framing_broken = False
        #: False for a successful EOF-delimited response or a successful
        #: CONNECT tunnel.  Those outcomes are not framing errors, but the
        #: HTTP/1.1 connection cannot be used for another request.
        self.reusable = True
        #: A successful CONNECT changes the transport into a tunnel.  It is
        #: non-reusable for HTTP, but closing the writer would discard the
        #: tunnel rather than merely retiring the HTTP protocol.
        self.tunnel = False
        #: True after either a successful CONNECT or a 101 response.  HTTP
        #: parsing is over, but the transport and any read-ahead bytes belong
        #: to the switched protocol and must remain available for handoff.
        self.protocol_switched = False
        #: Parsed response protocol version, retained for persistence policy.
        self.http_version: bytes | None = None
        #: Persistence of the response head alone.  A protocol switch always
        #: retires HTTP parsing, but only a persistent response is eligible to
        #: hand its still-open transport to another protocol.
        self.response_persistent = True
        #: The high-level sender sets this when its request included
        #: ``Connection: close``.  It is deliberately separate from response
        #: body framing.
        self.request_close = False
        #: Built on the first body read from the settings then in force and
        #: kept for the message: a window that did not span reads could not
        #: measure a rate.  One recipient per response, so it needs no reset.
        self._rate_floor: ByteRateFloor | None = None
        #: Set by the first read that delivered a body octet.  Until then the
        #: peer is thinking, not dripping, and the floor is not watching.
        self._body_open = False
        #: A short window seen on a read that delivered no payload.  Not yet a
        #: verdict — see :meth:`_body_read`.
        self._unpaid_framing = False
        #: Set when the message just read was delimited by the connection
        #: close (RFC 9112 §6.3 item 8).  Nothing is desynced — the body ended
        #: exactly where the peer said it would — but its delimiter *is* the
        #: connection's end, so there is no second message to read.
        self.connection_exhausted = False

    def _refuse_if_broken(self) -> None:
        if self.framing_broken:
            raise ConnectionError(
                'this reader was abandoned part-way through a message — '
                'its position in the byte stream is unknown')
        if not self.reusable:
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
            self.framing_broken = True
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
            self.framing_broken = True
            raise

    @staticmethod
    async def _read_head(reader: AbstractReader) -> bytes:
        """The whole response head, bounded in all three triad columns.

        ``read_head`` is the reader contract's own bounded head read, so the
        total column costs a budget rather than a mechanism, and every reader
        under the client answers it the same way.

        The per-line rule runs over the returned head instead of during the
        read.  No line can be longer than the block containing it, so the
        total has already capped what a single field can accumulate; what is
        left is a policy the caller chose, and the server draws the same line
        between ``header_max_total`` and ``header_max_line``.
        """
        cfg = get_settings()
        timeout = cfg.client_head_timeout
        try:
            if timeout > 0:
                async with asyncio.timeout(timeout):
                    head = await reader.read_head(cfg.client_head_max_total)
            else:
                head = await reader.read_head(cfg.client_head_max_total)
        except ReadLimitExceeded as exc:
            raise ResponseTooLarge(
                f'response head exceeds '
                f'BB_CLIENT_HEAD_MAX_TOTAL={cfg.client_head_max_total}',
                exc.seen) from None
        except IncompleteReadError as exc:
            raise ConnectionError('connection closed before response') from exc
        if not head:
            raise ConnectionError('connection closed before response')

        max_line = cfg.client_head_max_line
        # One comparison retires the walk for every head under the budget.
        if max_line > 0 and len(head) > max_line:
            for line in head.split(_CRLF):
                if len(line) > max_line:
                    raise ResponseTooLarge(
                        f'response header line {len(line)} bytes > '
                        f'BB_CLIENT_HEAD_MAX_LINE={max_line}', head[:max_line])
        return head

    async def _read_start(
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
                    or not 100 <= status < 200):
                return version, status, headers
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
        lines = head.split(_CRLF)
        # "HTTP/1.1 200 OK" — split into version, status, reason.
        parts = lines[0].split(b' ', 2)
        if len(parts) < 2:
            raise ProtocolError(f'malformed status line: {lines[0]!r}')
        version = parts[0]
        if version not in (b'HTTP/1.0', b'HTTP/1.1'):
            raise ProtocolError(f'unsupported response version: {version!r}')
        # RFC 9112 §4: status-code is exactly three ASCII decimal digits.
        if (len(parts[1]) != 3 or not parts[1].isdigit()
                or not parts[1].isascii()):
            raise ProtocolError(f'invalid status code: {parts[1]!r}')
        status = int(parts[1])

        pairs: list[tuple[bytes, bytes]] = []
        for line in lines[1:]:
            if not line:
                break
            name, separator, value = line.partition(b':')
            if not separator:
                raise ProtocolError(f'malformed response field: {line!r}')
            # bytes.strip() would also take VT, FF, CR and LF, turning an
            # invalid value into a valid framing instruction (VT + "chunked").
            value = value.strip(b' \t')
            if value.translate(None, _FIELD_VCHAR):
                raise ProtocolError(
                    f'prohibited control in response field {name!r}')
            pairs.append((name.strip(b' \t').lower(), value))
        return version, status, Headers.from_lowered(pairs)

    @staticmethod
    def _connection_options(headers: Headers) -> set[bytes]:
        """Parse repeated/comma-combined ``Connection`` token fields."""
        options: set[bytes] = set()
        for _, value in headers.getlist(b'connection'):
            for raw in value.split(b','):
                option = raw.strip(b' \t')
                if not option:
                    continue
                if not _TCHAR.issuperset(option):
                    raise ProtocolError(
                        f'invalid Connection option {option!r}')
                options.add(option.lower())
        return options

    @classmethod
    def _response_is_persistent(cls, version: bytes,
                                headers: Headers) -> bool:
        options = cls._connection_options(headers)
        if b'close' in options:
            return False
        if version == b'HTTP/1.0':
            return b'keep-alive' in options
        return True

    @staticmethod
    def _declared_length(headers: Headers) -> int | None:
        """The response's ``Content-Length``, or ``None`` when it has none.

        A response may repeat the field or comma-combine it, and RFC 9110
        §8.6 permits that **only** when every value agrees.  Trusting the
        first one is a desync: believe 5 where the peer meant 10 and the
        surplus five octets become the next keep-alive response's status
        line.  The server refuses exactly this on the request side
        (``_validate_message_framing``); this is the same rule facing the
        other way, including the leading-zero normalisation that makes
        "005" and "5" agree.
        """
        raw = headers.getlist(b'content-length')
        if not raw:
            return None
        values: set[bytes] = set()
        for _, value in raw:
            for v in value.split(b','):
                v = v.strip()
                if not v or not v.isdigit():
                    raise ProtocolError(f'invalid Content-Length value {v!r}')
                values.add(v.lstrip(b'0') or b'0')
        if len(values) > 1:
            raise ProtocolError(
                f'conflicting Content-Length values in response: '
                f'{sorted(values)!r}')
        return int(values.pop())

    async def _body_read(self, coro, *, payload: bool = False,
                         allow_eof: bool = False):
        """One body read, under both of the body's time bounds.

        ``BB_CLIENT_BODY_TIMEOUT`` is per read, not per body — the peer must
        keep making progress, which is what ``body_timeout`` means on the
        server and ``client_body_timeout`` in nginx.  It stops a peer that
        **stops**; one that trickles satisfies every individual read, which is
        why ``BB_CLIENT_MIN_BODY_RATE`` exists.

        *payload* is the floor's numerator and the reason it means anything:
        only response-body octets count, because chunk-size lines, extensions,
        terminators and trailers are discarded on receipt and a peer can pad
        them at will.  The denominator is every read's wait, framing reads
        included — a peer that stalls in front of the octets that are not
        counted must not buy free time with the gap.  This is the one place a
        body read waits, so the seconds measured here are transport wait: a
        caller slow between ``stream()`` yields is never mistaken for a peer
        slow to send.

        Truncation is named here too: ``AbstractReader.readexactly`` returns
        short at EOF while ``AsyncioReader`` raises, and neither answer
        belonged to the client's exception family.
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
                async with asyncio.timeout(timeout):
                    data = await coro
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
            # A framing read is judged one read too early.  The octets that
            # pay for its wait arrive in the same delivery as the chunk-size
            # line and are read on the *next* call, so a framing read that is
            # short has proved nothing yet — a peer sending 2 KiB/s against a
            # 1 KiB/s floor was refused for it.  A second read that still
            # cannot clear the window has: either payload arrived and was not
            # enough, or none is coming.  The seconds stay in the denominator
            # throughout, so nothing is forgiven — only deferred.
            if short and (payload or self._unpaid_framing):
                log_cap_hit('client_min_body_rate', requested=floor.observed,
                            limit=floor.rate, protocol='http1')
                raise TimeoutError(
                    f'response body arriving below '
                    f'BB_CLIENT_MIN_BODY_RATE={floor.rate} B/s')
            self._unpaid_framing = short
        return data

    @staticmethod
    def _method_is(request_method: str | bytes | HTTPMethod | None,
                   expected: str) -> bool:
        if isinstance(request_method, bytes):
            return request_method == expected.encode('ascii')
        return (request_method is not None
                and str(request_method) == expected)

    @staticmethod
    def _parse_transfer_encoding(
            fields: list[tuple[bytes, bytes]],) -> list[bytes]:
        """Parse repeated/comma-combined ``Transfer-Encoding`` fields.

        This is deliberately narrower than a general HTTP field parser: it
        returns only coding names because response framing does not interpret
        coding parameters.  It nevertheless validates the complete grammar so
        a malformed ignored parameter cannot hide a different final coding.
        Empty list members are accepted within a fixed bound, as permitted by
        the HTTP list rules used by the client for compatibility.
        """
        codings: list[bytes] = []
        empty_members = 0
        skip_ows, token, quoted_string = _skip_ows, _te_token, _te_quoted_string

        for _, value in fields:
            pos = 0
            while True:
                pos = skip_ows(value, pos)
                if pos == len(value):
                    # An empty field and the member after a trailing comma are
                    # both harmless, but not an unbounded amount of work.
                    empty_members += 1
                    if empty_members > _MAX_EMPTY_TRANSFER_MEMBERS:
                        raise ProtocolError(
                            'too many empty Transfer-Encoding list members')
                    break
                if value[pos] == 0x2c:  # comma: empty member
                    empty_members += 1
                    if empty_members > _MAX_EMPTY_TRANSFER_MEMBERS:
                        raise ProtocolError(
                            'too many empty Transfer-Encoding list members')
                    pos += 1
                    continue

                coding, pos = token(value, pos)
                pos = skip_ows(value, pos)
                while pos < len(value) and value[pos] == 0x3b:  # ';'
                    pos = skip_ows(value, pos + 1)
                    _, pos = token(value, pos)
                    pos = skip_ows(value, pos)
                    if pos >= len(value) or value[pos] != 0x3d:  # '='
                        raise ProtocolError(
                            'Transfer-Encoding parameter requires "="')
                    pos = skip_ows(value, pos + 1)
                    if pos < len(value) and value[pos] == 0x22:
                        pos = quoted_string(value, pos)
                    else:
                        _, pos = token(value, pos)
                    pos = skip_ows(value, pos)
                codings.append(coding.lower())
                if pos == len(value):
                    break
                if value[pos] != 0x2c:
                    raise ProtocolError(
                        'invalid Transfer-Encoding list separator')
                pos += 1

        return codings

    @classmethod
    def _body_framing(cls, status: int, headers: Headers,
                      request_method: str | bytes | HTTPMethod | None
                      ) -> tuple[str, int | None, bool, bool]:
        """Return ``(mode, declared_length, reusable, tunnel)``.

        RFC 9112 §6.3 gives method/status precedence over all response
        framing fields.  Transfer-Encoding also takes precedence over
        Content-Length; a non-chunked final coding therefore remains
        close-delimited rather than accidentally trusting Content-Length.
        """
        body_forbidden = (
            100 <= status < 200 or status in (204, 304)
            or cls._method_is(request_method, 'HEAD')
        )
        successful_connect = (
            cls._method_is(request_method, 'CONNECT')
            and 200 <= status < 300
        )
        protocol_switched = status == 101 or successful_connect
        if body_forbidden or successful_connect:
            return 'none', None, not protocol_switched, protocol_switched

        transfer_fields = headers.getlist(b'transfer-encoding')
        if transfer_fields:
            if headers.getlist(b'content-length'):
                # RFC 9112 §6.3 item 3 gives the precedence *and* says the
                # message "ought to be handled as an error".  Taking the
                # precedence alone leaves the client trusting whichever field
                # an intermediary in front of us did not, which is the
                # response-splitting shape.  The server refuses this exact
                # combination on the request side; one repository should not
                # hold two answers to one field pair.
                raise ProtocolError(
                    'Content-Length and Transfer-Encoding both present in '
                    'the response (response-splitting vector)')
            codings = cls._parse_transfer_encoding(transfer_fields)
            if codings.count(b'chunked') > 1:
                raise ProtocolError(
                    f'chunked applied more than once: {codings!r}')
            if codings and codings[-1] == b'chunked':
                return 'chunked', None, True, False
            # Transfer-Encoding overrides Content-Length even when the
            # final coding is not chunked; the message ends at connection EOF.
            # This includes a physically present field whose list members are
            # all empty: an empty parsed list does not erase TE's precedence
            # over Content-Length.
            return 'close', None, False, False

        declared = cls._declared_length(headers)
        if declared is not None:
            return 'declared', declared, True, False
        return 'close', None, False, False

    def _record_framing(self, framing: tuple[str, int | None, bool, bool], *,
                        request_method: str | bytes | HTTPMethod | None,
                        response_persistent: bool) -> None:
        """The one place the connection's fate is decided.

        Framing says whether this message delimits itself; ``Connection`` says
        whether the peer means to keep the connection.  Reuse needs both, so
        both are spent here — not assigned by the body reader and corrected by
        its caller afterwards, which is one invariant in two places under two
        different operators.
        """
        mode, _declared, framing_reusable, tunnel = framing
        self.reusable = framing_reusable and response_persistent
        self.protocol_switched = tunnel
        self.tunnel = tunnel and self._method_is(request_method, 'CONNECT')
        self.connection_exhausted = mode == 'close'

    async def _read_head_and_policy(
            self, reader: AbstractReader, *, skip_interim: bool = True,
            method: str | bytes | HTTPMethod | None = None,
    ) -> tuple[int, Headers, tuple[str, int | None, bool, bool]]:
        """Read the head and settle everything the body read must not decide."""
        version, status, headers = await self._read_start(
            reader, skip_interim=skip_interim)
        self.http_version = version
        response_persistent = self._response_is_persistent(version, headers)
        self.response_persistent = response_persistent
        request_method = (method if method is not None
                          else self.request_method)
        framing = self._body_framing(status, headers, request_method)
        self._record_framing(framing, request_method=request_method,
                             response_persistent=response_persistent)
        return status, headers, framing

    async def _read_body(self, reader: AbstractReader,
                         framing: tuple[str, int | None, bool, bool]) -> bytes:
        # The total lives here and not in ``_read_chunked``, which
        # ``_stream_body`` shares: this is the path that accumulates, and the
        # other one exists precisely so a large response need not.
        max_total = get_settings().client_body_max_total
        mode, declared, _reusable, _tunnel = framing
        if mode == 'none':
            return b''
        if mode == 'chunked':
            return b''.join([c async for c in
                             self._read_chunked(reader, max_total=max_total)])
        if mode == 'declared':
            assert declared is not None
            if max_total and declared > max_total:
                # Refused on the declaration, before an octet is read — the
                # peer already told us it will not fit.
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
        mode, declared, _reusable, _tunnel = framing
        if mode == 'none':
            return
        if mode == 'chunked':
            async for chunk in self._read_chunked(reader):
                yield chunk
            return
        if mode == 'declared':
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
        """A ``Content-Length`` body, in transport-paced slices.

        Slices, so a large response need not fit in memory: a single exact
        read hands the caller the whole body as one chunk, which is the
        memory bound missing on exactly the path that asked for it.

        Up-to-n rather than ``readexactly``, because an exact read loops
        internally until its slice is full and every bound above it therefore
        sees one read.  On the buffering path that made
        ``BB_CLIENT_BODY_TIMEOUT`` the deadline for the entire body, so a
        large response that never once stopped arriving was refused for
        outlasting what one read is allowed — while its own documentation
        promised a per-read progress deadline.  Transport-paced reads return
        whatever arrived, which is the shape and the reason of the server's
        ``body_chunk_max`` path.
        """
        remaining = declared
        while remaining > 0:
            chunk = await self._body_read(
                reader.read(min(remaining, _STREAM_CHUNK_SIZE)), payload=True)
            if not chunk:
                # A short-reading reader answers EOF with b'', so subtracting
                # it left the loop spinning on a condition nothing could
                # change — and with no await in the reader, uncancellable.
                raise ConnectionError('connection closed mid-body')
            remaining -= len(chunk)
            yield chunk

    async def _read_framing_line(self, reader: AbstractReader,
                                 limit: int) -> bytes:
        """One chunk-framing line — chunk-size line or trailer field line.

        Bounded by the same budget as a header field line.  A chunk-*ext* and
        a trailer field are discarded on receipt, so nothing legitimate needs
        more; the chunk-*size* is not discarded, but no legitimate one is
        anywhere near this long either.

        Not every reader's ``readuntil`` takes the budget.  ``AbstractReader``
        ships ``_accepts_read_limit`` for exactly that, and ``read_head``
        consults it; passing the budget positionally and unconditionally
        raised ``TypeError`` on a one-argument reader — and, because
        ``receive`` marks the framing broken on any exception, abandoned the
        connection along with it.  Falling back must not fall open, so the
        default bounded implementation carries the same budget.  The answer is
        cached on the reader, as ``read_head`` caches it, so the question is
        asked once per connection rather than once per framing line.
        """
        native = reader.__dict__.get('_readuntil_accepts_limit')
        if native is None:
            native = _accepts_read_limit(reader.readuntil)
            try:
                reader.__dict__['_readuntil_accepts_limit'] = native
            except (AttributeError, TypeError):
                pass  # a __slots__ reader answers the question every time.
        try:
            if native:
                return await self._body_read(reader.readuntil(_CRLF, limit))
            return await self._body_read(
                AbstractReader._readuntil_bounded(reader, _CRLF, limit))
        except ReadLimitExceeded as exc:
            raise ResponseTooLarge(
                f'chunk framing line exceeds '
                f'BB_CLIENT_HEAD_MAX_LINE={limit}', exc.seen) from None
        except asyncio.LimitOverrunError as exc:
            # With the budget switched off the reader falls back to whatever
            # limit it has of its own and reports it in its own vocabulary.
            # Same condition, so the same answer leaves the client — a raw
            # asyncio error is not part of its exception family.
            raise ResponseTooLarge(
                f'chunk framing line exceeds the reader buffer '
                f'({exc.consumed} bytes)') from None

    @staticmethod
    def _parse_chunk_size(size_line: bytes) -> int:
        """The chunk-size numeral, by the grammar rather than by ``int``.

        No digit ceiling.  RFC 9112 §7.1 asks recipients to *anticipate*
        potentially large hexadecimal numerals and not to lose precision on
        them, which Python's arbitrary-precision ``int`` already satisfies;
        reading that as licence to reject long numerals would refuse
        conforming wire, and ``last-chunk = 1*("0")`` puts no ceiling on the
        zeros either.  The line length is already bounded by the caller, and
        a declared size too large to satisfy is refused where the octets are
        counted, not where the numeral is read.

        The terminator is required rather than stripped, which is what makes
        a bare CR inside the element fail: stripping every trailing CR/LF
        deleted it and let the rest parse as though it were clean.  RFC 9112
        §2.2 gives a recipient of a bare CR two options — treat the element as
        invalid, or replace it with SP — and a replaced SP leaves a numeral
        that is not ``1*HEXDIG``, so refusal is the only conforming outcome
        either way.  A line that reached EOF without its CRLF is refused by
        the same check.

        ``BWS`` is removed only where the grammar has it: ``chunk-ext =
        *( BWS ";" BWS chunk-ext-name … )``, so whitespace is legal before a
        ``;`` and nowhere else.  RFC 9110 §5.6.3 makes removing it a MUST;
        a bare ``5 \r\n`` with no extension has no BWS to remove and stays a
        smuggling vector.  Mirrors the server's parser, which is the oracle
        the tests compare against.
        """
        if not size_line.endswith(_CRLF) or size_line.count(b'\n') != 1:
            raise ProtocolError(
                f'chunk-size line not CRLF-terminated: {size_line!r}')
        numeral, sep, _ext = size_line[:-2].partition(b';')
        if sep:
            numeral = numeral.rstrip(b' \t')
        if not numeral or not _HEXDIG.issuperset(numeral):
            raise ProtocolError(f'invalid chunk size: {size_line!r}')
        return int(numeral, 16)

    async def _read_trailer_section(self, reader: AbstractReader,
                                    line_max: int, total_max: int) -> None:
        """Consume the trailer section whole (RFC 9112 §7.1.2).

        Reading one line assumed the section was empty.  With real trailers
        the rest stayed buffered, so the next keep-alive response began
        parsing at a trailer field line and took it for a status line — the
        response-side twin of the desync ``_declared_length`` guards against.

        Discarded, not surfaced: nothing on ``ClientResponse`` carries them,
        and inventing a field for them here would be a second decision hiding
        inside a framing fix.
        """
        total = 0
        while True:
            line = await self._read_framing_line(reader, line_max)
            if not line.rstrip(_CRLF):
                return
            total += len(line)
            if total_max and total > total_max:
                raise ResponseTooLarge(
                    f'trailer section exceeds '
                    f'BB_CLIENT_HEAD_MAX_TOTAL={total_max}')

    async def _read_chunked(self, reader: AbstractReader, *,
                            max_total: int = 0) -> AsyncIterator[bytes]:
        cfg = get_settings()
        line_max = cfg.client_head_max_line
        seen = 0
        while True:
            size = self._parse_chunk_size(
                await self._read_framing_line(reader, line_max))
            if size == 0:
                await self._read_trailer_section(
                    reader, line_max, cfg.client_head_max_total)
                return
            seen += size
            if max_total and seen > max_total:
                raise ResponseTooLarge(
                    f'body exceeds BB_CLIENT_BODY_MAX_TOTAL={max_total}')
            # In slices, so one peer-declared chunk-size is never one
            # allocation and one chunk is not one read.
            remaining = size
            while remaining > 0:
                piece = await self._body_read(
                    reader.read(min(remaining, _STREAM_CHUNK_SIZE)),
                    payload=True)
                if not piece:
                    raise ConnectionError('connection closed mid-chunk')
                remaining -= len(piece)
                yield piece
            # Exactly CRLF, not read-until: reading until would swallow spill
            # up to the next one and tolerate a bare CR or LF terminator —
            # what the server refuses as SMUG-CHUNK-SPILL.
            term = await self._body_read(reader.readexactly(2))
            if term != _CRLF:
                raise ProtocolError(
                    f'chunk-data not CRLF-terminated: {term!r}')


def _record_response(result, response) -> None:
    """Log one response: newest in ``response``, all of them in ``received``.

    ``response`` keeps its old meaning (the most recent read) so existing
    scenarios are untouched; ``received`` is what a scenario needs when the
    peer sends more than one thing.
    """
    result.response = response
    result.received.append(response)
    body = getattr(response, 'body', b'') or b''
    result.server_bytes_received += len(body)


class HTTP1UpgradeSession:
    """Bidirectional transport after CONNECT or an HTTP 101 switch.

    Obtain one with :meth:`HTTP1Client.handoff`.  The handoff is one-shot:
    once returned, this session owns transport closure and the originating
    HTTP client can no longer read or write the connection.
    """

    def __init__(self, reader: AbstractReader, writer: AbstractWriter,
                 raw_writer: asyncio.StreamWriter) -> None:
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


class HTTP1Client:
    """Async HTTP/1.1 client.

    Use as an async context manager::

        async with HTTP1Client('localhost', 8000) as c:
            res = await c.request(HTTPMethod.GET, '/path')

    The connection persists across multiple ``request()`` calls when HTTP
    version, Connection options, and body framing permit it.  A successful
    CONNECT or 101 can be transferred with :meth:`handoff`; after that, the
    returned :class:`HTTP1UpgradeSession` owns transport closure.  Pass
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
        self._raw_writer: asyncio.StreamWriter | None = None
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
        #: Set once a response read stopped part-way.  See :meth:`_abandon`.
        self._framing_broken = False
        #: A successful close-delimited response and a successful CONNECT
        #: switch protocols at EOF / the HTTP tunnel boundary.  They are not
        #: framing errors, but neither permits another HTTP/1.1 request.
        self._reusable = True
        #: The response body currently owns the reader.  This remains set
        #: between stream yields; ``_reusable`` alone cannot represent that
        #: intermediate state because a self-delimited response is reusable
        #: only after its delimiter has been consumed.
        self._active_response: HTTP1ResponseRecipient | None = None
        #: True only when close-delimited framing consumed the connection.
        #: Kept separate from other valid non-reusable outcomes so diagnostics
        #: name the actual lifecycle mechanism.
        self._connection_exhausted = False
        #: A CONNECT/101 response permanently ends HTTP parsing, regardless of
        #: whether persistence policy permits preserving its transport.
        self._protocol_switched = False
        #: Set after CONNECT/101 has completed and cleared by the one-shot
        #: public handoff.  Until handoff, this client still owns closure.
        self._handoff_ready = False
        self._transport_handed_off = False
        self._closed = False

    # ---- async context manager -------------------------------------------

    async def __aenter__(self) -> 'HTTP1Client':
        if self._raw_writer is None:
            r, w = await _open_connection(self._host, self._port, self._ssl,
                                          self._connect_timeout)
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
        self._closed = True
        # A session can only be created while this client still owns a live
        # transport.  Context exit is irreversible even if close itself is
        # best-effort or the peer closed first.
        self._handoff_ready = False
        if self._raw_writer is not None:
            try:
                self._raw_writer.close()
                await self._raw_writer.wait_closed()
            except Exception:
                pass  # best-effort close on context exit; peer may already be gone.

    # ---- public API ------------------------------------------------------

    def _abandon(self) -> None:
        """Stop using this connection: its place in the byte stream is lost.

        A read that stopped part-way leaves the rest of the message on the
        wire, so the next response read would begin inside it — and a peer
        whose body is itself a well-formed response gets one delivered for a
        request the server answered differently.  The server answers the same
        situation by closing rather than by keep-aliving a desynced stream.

        Deliberately not applied to :meth:`read_response`, the fault-injection
        primitive: driving a misbehaving peer and then looking at what else it
        sent is what that method is for.
        """
        self._framing_broken = True
        self._reusable = False
        self._handoff_ready = False
        if self._raw_writer is not None:
            try:
                self._raw_writer.close()
            except Exception:
                pass  # best-effort: the peer may already be gone.

    def _close_nonreusable(self, *, connection_exhausted: bool = False) -> None:
        """Close after a valid response that consumed the connection."""
        self._reusable = False
        self._handoff_ready = False
        self._connection_exhausted = (
            self._connection_exhausted or connection_exhausted)
        if self._raw_writer is not None:
            try:
                self._raw_writer.close()
            except Exception:
                pass  # best-effort: the peer may already be gone.

    def _enter_protocol_switch(self, *, handoff_allowed: bool) -> None:
        """Retire HTTP and preserve only a policy-persistent transport."""
        self._reusable = False
        self._protocol_switched = True
        self._handoff_ready = handoff_allowed and not self._closed
        if not self._handoff_ready and self._raw_writer is not None:
            try:
                self._raw_writer.close()
            except Exception:
                pass  # best-effort retirement after a valid switch response.

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
        if recipient.framing_broken:
            if abandon_incomplete:
                self._abandon()
            return
        elif not complete:
            if abandon_incomplete:
                # Closing between yields has not proved that the body
                # delimiter was consumed, even if the previous transport read
                # happened to contain the final octets.
                self._abandon()
        elif recipient.protocol_switched:
            self._enter_protocol_switch(handoff_allowed=(
                recipient.response_persistent and not recipient.request_close))
        elif recipient.request_close or not recipient.reusable:
            self._close_nonreusable(
                connection_exhausted=recipient.connection_exhausted)

    def _refuse_if_desynced(self) -> None:
        if self._closed:
            raise ConnectionError('HTTP1Client context is closed')
        if self._active_response is not None:
            raise ConnectionError(
                'a response body is still being consumed on this connection')
        if self._framing_broken:
            raise ConnectionError(
                'connection abandoned after a framing error — '
                'its position in the byte stream is unknown')
        if self._connection_exhausted:
            raise ConnectionError(
                'the last response was delimited by the connection close — '
                'this connection cannot carry another request')
        if not self._reusable:
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
        if self._closed:
            raise ConnectionError('HTTP1Client context is closed')
        if self._transport_handed_off:
            raise ConnectionError('the switched transport was already handed off')
        if not self._handoff_ready:
            raise ConnectionError(
                'no completed CONNECT or 101 response is available for handoff')
        assert self._reader is not None and self._writer is not None
        assert self._raw_writer is not None
        session = HTTP1UpgradeSession(
            self._reader, self._writer, self._raw_writer)
        self._handoff_ready = False
        self._transport_handed_off = True
        # The session now owns close.  In particular HTTP1Client.__aexit__
        # must not close a transport that has escaped its context.
        self._raw_writer = None
        return session

    # ---- Low-level test-instrument primitives ----------------------------
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
        if self._transport_handed_off:
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

    async def read_response(
            self, *,
            request_method: str | bytes | HTTPMethod | None = None,
            timeout: float | None = None) -> ClientResponse:
        """Read one HTTP/1.1 response from the connection.

        Optional ``timeout`` bounds the entire read (status line + headers
        + body).  Raises :class:`asyncio.TimeoutError` if the deadline is
        hit; the caller decides whether to treat that as a transport-
        fail or a normal protocol outcome."""
        if self._closed:
            raise ConnectionError('HTTP1Client context is closed')
        if not self._reusable:
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

    # ---- Scenario executor -----------------------------------------------
    #
    # A :class:`Scenario` is a tagged sequence of steps (SendBytes / Sleep /
    # ReadResponse / Abort) shared by the differential test and the atheris
    # fuzz harness.  The executor below is glue over the low-level primitives:
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
                        _record_response(
                            result,
                            await self.read_response(timeout=step.timeout))
                    except asyncio.TimeoutError as exc:
                        result.timed_out = True
                        result.exception = repr(exc)
                        return result
                elif isinstance(step, WaitForResponse):
                    # A filter: read past what does not match, counting it.
                    # On HTTP/1.1 a non-zero count is worth reading — replies
                    # come back in request order, so a skip means the
                    # scenario is further along the pipeline than it thinks.
                    deadline = _time.monotonic() + step.timeout
                    while True:
                        remaining = deadline - _time.monotonic()
                        if remaining <= 0:
                            result.wait_timed_out = True
                            break
                        try:
                            got = await self.read_response(timeout=remaining)
                        except asyncio.TimeoutError:
                            result.wait_timed_out = True
                            break
                        _record_response(result, got)
                        if response_matches(got, step.match):
                            break
                        result.wait_skipped += 1
                elif isinstance(step, ExpectResponse):
                    # A guard: one response, nothing skipped, and the
                    # verdict recorded either way — a scenario whose
                    # premise silently failed would look like a pass.
                    try:
                        got = await self.read_response(timeout=step.timeout)
                    except asyncio.TimeoutError:
                        result.wait_timed_out = True
                        result.expectations.append((dict(step.match), False))
                    else:
                        _record_response(result, got)
                        result.expectations.append(
                            (dict(step.match),
                             response_matches(got, step.match)))
                elif isinstance(step, HalfClose):
                    # FIN, not RST, and deliberately not terminal: a
                    # half-closed client is still reading, which is the
                    # only reason to script one.
                    result.half_closed = _sc_half_close(self._raw_writer)
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
