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
    in memory — true of both framings, which it was not until the
    ``Content-Length`` path stopped reading the body in one call.
    """

    def __init__(self) -> None:
        #: Set when a read failed part-way through a message, which makes the
        #: reader's position unknown.  The connection is then unusable: the
        #: rest of the message is still on the wire, so the next read would
        #: begin inside it and parse a body as a response.  The server's
        #: recipient carries the same flag for the same reason.
        self.framing_broken = False
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

    async def receive(self, reader: AbstractReader, *,
                      method: str | HTTPMethod | None = None,
                      skip_interim: bool = True) -> ClientResponse:
        """One complete response.

        *method* is the request's, and it is not decoration: RFC 9112 §6.3
        decides the body's length from the status code **and** the method, so
        a recipient handed only the header fields cannot apply the first and
        most overriding of its rules.  ``None`` means the caller does not know
        — the fault-injection primitives drive peers whose request never went
        through this client — and only the status-code half is then applied.

        *skip_interim* is what RFC 9110 §15.2 requires, and every production
        call wants it.  The fault-injection primitives do not: their whole
        purpose is to drive a peer and see what it actually sent, and a reader
        that silently discards part of that answers a different question.  The
        same distinction ``_abandon`` already makes for those primitives.
        """
        self._refuse_if_broken()
        try:
            status, headers = await self._read_start(
                reader, skip_interim=skip_interim)
            body = await self._read_body(reader, headers,
                                         status=status, method=method)
        except BaseException:
            # Every refusal below this point leaves unread octets behind, and
            # a peer whose body is itself a well-formed response gets one
            # delivered for a request the server answered differently.  Which
            # error it was does not matter — what matters is that we stopped
            # somewhere the peer chose.
            self.framing_broken = True
            raise
        return ClientResponse(status=status, headers=headers, body=body)

    async def stream(self, reader: AbstractReader, *,
                     method: str | HTTPMethod | None = None) -> AsyncIterator[bytes]:
        # Body-only streaming: callers that need status/headers should use
        # ``receive``.  Yielding the start-line as the first item would force
        # callers to special-case the iterator's first element.  The status is
        # not returned but is still needed, because it is half of what decides
        # whether there is a body to yield at all.
        self._refuse_if_broken()
        try:
            status, headers = await self._read_start(reader)
            async for chunk in self._stream_body(reader, headers,
                                                 status=status, method=method):
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

    async def _read_start(self, reader: AbstractReader, *,
                          skip_interim: bool = True) -> tuple[int, Headers]:
        """The **final** response's status line and header fields.

        RFC 9110 §15.2: *"A client MUST be able to parse one or more 1xx
        responses received prior to a final response, even if the client does
        not expect one.  A user agent MAY ignore unexpected 1xx responses."*
        The MAY is *ignore*; the MUST is *reach the final response*.  Returning
        the interim as the answer does neither — the caller gets a status it
        never asked about while the real response is still on the wire, which
        is the desync shape the trailer fix and the refusal fix closed from
        their own directions.  ``103 Early Hints`` is served by Cloudflare and
        Fastly today, so the peer that does this is an ordinary one.

        Ignored rather than surfaced, deliberately.  An ``Early Hints`` link
        set is worth having and a ``100 Continue`` matters to a sender waiting
        on it, but this client sends no ``Expect``, and exposing the interims
        means widening ``ClientResponse``, which HTTP/2 shares and reaches by
        another route.  Discarding is what the MAY permits; what was wrong was
        not discarding them but returning one.

        ``101`` is 1xx by number and final by meaning: the connection switches
        protocols, so nothing this parser should read follows it.  The
        WebSocket handshake reads its ``101`` through here.

        The triad, because the loop moves where two of its columns sit.
        ``client_head_max_total`` and ``client_head_max_line`` still bound one
        head, and each interim is a head, so the *unit* is unchanged.
        ``client_head_timeout`` is likewise per head — and must be, since a
        ``103 Early Hints`` exists precisely so a peer can say "still working"
        before it answers, which is the wait the deadline is exempt from for
        the same reason the body's progress deadline is re-armed by each
        frame.  What that leaves unowned is the aggregate, in both columns, and
        ``client_max_interim_responses`` is what owns it: at most
        ``limit + 1`` heads, so at most ``(limit + 1) x client_head_timeout``
        seconds and ``(limit + 1) x client_head_max_total`` octets read.  With
        the cap disabled there is no aggregate owner at all, which is what
        setting it to 0 buys.
        """
        cfg = get_settings()
        limit = cfg.client_max_interim_responses
        seen = 0
        while True:
            status, headers = await self._read_message_head(reader)
            if not (100 <= status < 200) or status == 101 or not skip_interim:
                return status, headers
            # Interim responses are the count axis in miniature: each head is
            # small, arrives whole and promptly, and carries no body, so it
            # satisfies the head total, the head deadline and every body
            # bound.  Only how *many* there are is anomalous, and no size or
            # deadline can see that.
            seen += 1
            if limit and seen > limit:
                log_cap_hit('client_max_interim_responses', requested=seen,
                            limit=limit, protocol='http1')
                raise ResponseTooLarge(
                    f'peer sent more than '
                    f'BB_CLIENT_MAX_INTERIM_RESPONSES={limit} interim '
                    f'responses without a final one')

    async def _read_message_head(self,
                                 reader: AbstractReader) -> tuple[int, Headers]:
        head = await self._read_head(reader)
        lines = head.split(_CRLF)
        # "HTTP/1.1 200 OK" — split into version, status, reason.
        parts = lines[0].split(b' ', 2)
        if len(parts) < 2:
            raise ProtocolError(f'malformed status line: {lines[0]!r}')
        # RFC 9112 §4: ``status-code = 3DIGIT``.  ``int()`` is laxer in the
        # same ways ``int(x, 16)`` was on the chunk-size numeral — a sign, an
        # underscore separator, surrounding whitespace — and the status no
        # longer only gets reported: §6.3 items 1 and 2 let it decide whether a
        # body is read at all, so ``2_0_4`` would suppress a body that an
        # intermediary enforcing the grammar had framed.
        if len(parts[1]) != 3 or not parts[1].isdigit() or not parts[1].isascii():
            raise ProtocolError(f'invalid status code: {parts[1]!r}')
        status = int(parts[1])

        pairs: list[tuple[bytes, bytes]] = []
        for line in lines[1:]:
            if not line:
                break
            name, _, value = line.partition(b':')
            pairs.append((name.strip().lower(), value.strip()))
        return status, Headers(pairs)

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

    async def _body_read(self, coro, *, payload: bool = False):
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
    def _has_no_body(status: int,
                     method: 'str | bytes | HTTPMethod | None') -> bool:
        """RFC 9112 §6.3 items 1 and 2, which override every header field
        present and therefore run before any of them is read.

        > 1. Any response to a HEAD request and any response with a 1xx, 204,
        >    or 304 status code is always terminated by the first empty line
        >    after the header fields, regardless of the header fields present
        >    in the message, and thus cannot contain a message body or trailer
        >    section.
        > 2. Any 2xx response to a CONNECT request implies that the connection
        >    will become a tunnel immediately after the empty line that
        >    concludes the header fields.  A client MUST ignore any
        >    Content-Length or Transfer-Encoding header fields received in
        >    such a message.

        A ``CONNECT`` that *failed* is an ordinary response and keeps its
        body: only 2xx opens the tunnel.

        None of this was reachable, because the decision was handed the header
        fields alone.  A ``204`` carrying ``content-length: 5`` therefore read
        five octets — of the *next* response — and the response after it still
        parsed, out of what the theft left behind.  A desync that produces a
        plausible answer is worse than one that raises.
        """
        if 100 <= status < 200 or status in (204, 304):
            return True
        if method is None:
            return False
        # A bytes method reaches the low-level primitives, and ``str(b'HEAD')``
        # is ``"b'HEAD'"`` — a name that matches nothing, so the rule would
        # silently not apply rather than fail.
        name = (method.decode('latin-1') if isinstance(method, bytes)
                else str(method)).upper()
        return name == 'HEAD' or (name == 'CONNECT' and 200 <= status < 300)

    @classmethod
    def _body_delimiter(cls, headers: Headers) -> tuple[str, int]:
        """How this body ends, by RFC 9112 §6.3 items 3, 4, 6 and 8.

        Item 3 first, because it is the one that must not be resolved by
        preference: a message carrying both ``Transfer-Encoding`` and
        ``Content-Length`` *"might indicate an attempt to perform request
        smuggling … or response splitting and ought to be handled as an
        error"*.  The server refuses exactly this shape on the request side;
        this is the same rule facing the other way.

        Item 4 is a list, not a token.  ``te == b'chunked'`` was an exact
        match, so ``gzip, chunked`` — a legal coding list whose final coding is
        chunked — missed the chunked branch, fell through to
        ``Content-Length``, found none, and returned an empty body while a
        whole chunked message stayed on the wire.  The codings are flattened
        across comma-combined values and repeated fields, in order, because
        §6.1 permits both spellings of the same list.

        Empty elements are dropped before the list is judged, which RFC 9110
        §5.6.1.2 makes a MUST: *"A recipient of such a list that contains an
        empty element MUST treat it as if the empty element were not
        present."*  Judging them instead reads ``chunked,`` as a list whose
        final coding is the empty one — not chunked, therefore delimited by
        the close — so the chunked body **and every response pipelined behind
        it** became this response's body, with no error and no sign that a
        second response had existed.  That is the smuggling probe the rule
        exists to defeat, and dropping empties is also what makes the leading
        and trailing spellings agree.

        Applying chunked twice is refused rather than read.  RFC 9112 §7.1
        makes it a sender MUST NOT, so no conforming peer sends it, and the
        server already answers this exact field value on the request side; a
        recipient that instead read it one way while its own server read it
        another would hold two answers to one message.

        Non-chunked codings are not decoded: the octets are handed back as the
        transfer coding delivered them.  Length is this function's question,
        and refusing a conforming message because a coding above the framing
        is unfamiliar would answer a different one.
        """
        tes = headers.getlist(b'transfer-encoding')
        if not tes:
            declared = cls._declared_length(headers)
            if declared is None:
                return _UNTIL_CLOSE, 0
            return _DECLARED, declared
        if headers.getlist(b'content-length'):
            raise ProtocolError(
                'Content-Length and Transfer-Encoding both present in the '
                'response (response-splitting vector)')
        codings = [coding for coding in
                   (c.strip().lower()
                    for _, raw_value in tes for c in raw_value.split(b','))
                   if coding]
        if not codings:
            # ``transfer-coding`` is a ``1#`` list, so a field whose elements
            # are all empty has none, and a framing field that frames nothing
            # is not a message this reader can place.
            raise ProtocolError(
                'Transfer-Encoding field carries no transfer coding')
        if codings.count(b'chunked') > 1:
            raise ProtocolError(
                f'chunked applied more than once: {codings!r}')
        if codings[-1] == b'chunked':
            return _CHUNKED, 0
        # §6.3 item 4, response branch: chunked present but not the final
        # coding leaves the framing undeterminable, and for a response the RFC
        # names the remedy rather than an error — read until the close.
        return _UNTIL_CLOSE, 0

    async def _read_body(self, reader: AbstractReader, headers: Headers, *,
                         status: int,
                         method: 'str | HTTPMethod | None' = None) -> bytes:
        # The total lives here and not in ``_read_chunked``, which
        # ``_stream_body`` shares: this is the path that accumulates, and the
        # other one exists precisely so a large response need not.
        if self._has_no_body(status, method):
            return b''
        max_total = get_settings().client_body_max_total
        kind, declared = self._body_delimiter(headers)
        if kind == _CHUNKED:
            return b''.join([c async for c in
                             self._read_chunked(reader, max_total=max_total)])
        if kind == _UNTIL_CLOSE:
            return b''.join([c async for c in
                             self._read_until_close(reader,
                                                    max_total=max_total)])
        if max_total and declared > max_total:
            # Refused on the declaration, before an octet is read — the peer
            # already told us it will not fit.
            raise ResponseTooLarge(
                f'declared body {declared} bytes exceeds '
                f'BB_CLIENT_BODY_MAX_TOTAL={max_total}')
        # In slices, like the streaming path: one ``readexactly`` for the
        # whole body is a single read to every bound above it.
        return b''.join([chunk async for chunk in
                         self._read_declared(reader, declared)])

    async def _stream_body(self, reader: AbstractReader, headers: Headers, *,
                           status: int,
                           method: 'str | HTTPMethod | None' = None
                           ) -> AsyncIterator[bytes]:
        if self._has_no_body(status, method):
            return
        kind, declared = self._body_delimiter(headers)
        if kind == _CHUNKED:
            async for chunk in self._read_chunked(reader):
                yield chunk
            return
        if kind == _UNTIL_CLOSE:
            async for chunk in self._read_until_close(reader):
                yield chunk
            return
        async for chunk in self._read_declared(reader, declared):
            yield chunk

    async def _read_until_close(self, reader: AbstractReader, *,
                                max_total: int = 0) -> AsyncIterator[bytes]:
        """RFC 9112 §6.3 item 8 — a body whose only delimiter is the close.

        The branch this replaces returned an empty body and gave keep-alive as
        the reason.  It is the other way round: the octets *are* the response,
        and a message the close delimits has already spent the connection,
        which is what ``connection_exhausted`` records for the caller.

        The unit is the transport-paced read; the time is ``_body_read``'s
        per-read deadline, which is what stops a peer that sends a head and
        then neither sends nor closes.  The total is ``max_total``, enforceable
        only as the octets arrive because this is the one framing that declares
        nothing to refuse in advance — and passed only by the buffering entry
        point, exactly as ``_read_chunked`` is: ``stream()`` exists so a large
        response need not fit in memory, and a cap on the shared reader would
        cap the path that asked not to be capped.
        """
        self.connection_exhausted = True
        total = 0
        while True:
            chunk = await self._body_read(reader.read(_STREAM_CHUNK_SIZE),
                                          payload=True)
            if not chunk:
                return
            total += len(chunk)
            if max_total and total > max_total:
                log_cap_hit('client_body_max_total', requested=total,
                            limit=max_total, protocol='http1')
                raise ResponseTooLarge(
                    f'response body exceeds '
                    f'BB_CLIENT_BODY_MAX_TOTAL={max_total}')
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
        #: Set once a response was delimited by the close.  See :meth:`_spend`.
        self._connection_exhausted = False

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
        if self._raw_writer is not None:
            try:
                self._raw_writer.close()
            except Exception:
                pass  # best-effort: the peer may already be gone.

    def _spend(self) -> None:
        """The last response's delimiter was the close, so this is spent.

        Distinct from :meth:`_abandon`, and deliberately worded differently:
        nothing desynced — the body ended exactly where the peer said it
        would — but a message RFC 9112 §6.3 item 8 delimits has consumed the
        connection along with itself, so a second request would go into a
        socket the peer is closing.
        """
        self._connection_exhausted = True
        if self._raw_writer is not None:
            try:
                self._raw_writer.close()
            except Exception:
                pass  # best-effort: the peer is closing it from its end.

    def _refuse_if_desynced(self) -> None:
        if self._framing_broken:
            raise ConnectionError(
                'connection abandoned after a framing error — '
                'its position in the byte stream is unknown')
        if self._connection_exhausted:
            raise ConnectionError(
                'the last response was delimited by the connection close — '
                'this connection cannot carry another request')

    async def request(self, method: str | HTTPMethod, path: str, *,
                      headers: HeaderList = (),
                      body: RequestBody = b'') -> ClientResponse:
        self._refuse_if_desynced()
        assert self._writer is not None and self._reader is not None
        h = self._headers_with_host(headers)
        await HTTP1RequestSender(self._writer).send(method, path, h, body)
        recipient = HTTP1ResponseRecipient()
        try:
            return await recipient.receive(self._reader, method=method)
        finally:
            if recipient.framing_broken:
                self._abandon()
            elif recipient.connection_exhausted:
                self._spend()

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
        await HTTP1RequestSender(self._writer).send(method, path, h, body)
        recipient = HTTP1ResponseRecipient()
        try:
            async for chunk in recipient.stream(self._reader, method=method):
                yield chunk
        finally:
            if recipient.framing_broken:
                self._abandon()
            elif recipient.connection_exhausted:
                self._spend()

    def _headers_with_host(self, headers: HeaderList) -> Headers:
        h = Headers(list(headers))
        if b'host' not in h:
            h.append(b'host', f'{self._host}:{self._port}'.encode())
        return h

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
        # Interims are not skipped here.  This primitive exists to drive a
        # peer and report what it actually sent, so a ``100 Continue`` is an
        # observation rather than noise on the way to the answer — the same
        # exemption ``_abandon`` already makes for it.
        coro = HTTP1ResponseRecipient().receive(self._reader,
                                                skip_interim=False)
        if timeout is None:
            return await coro
        return await asyncio.wait_for(coro, timeout=timeout)

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
