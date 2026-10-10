"""HTTP/1.1 Actor classes for the BlackBull actor model.

HTTP1Actor drives the keep-alive loop for one TCP connection.
RequestActor owns the lifetime of a single HTTP request.
"""
import asyncio
import logging
import re
import time as _time
from base64 import b64encode, b64decode
from binascii import Error as BinasciiError
from collections.abc import Awaitable, Callable
from hashlib import sha1
from http import HTTPStatus

from ..actor import Actor, Message
from ..env import get_settings
from ..event_aggregator import EventAggregator
from ..asgi import ASGIReceiveCallable, ASGISendCallable
from ..connection import (
    Connection, bind_receive_channel)
from ..headers import Headers
from ..protocol.framing import (method_is, parse_content_length,
                                split_transfer_codings)
from .deadline import ConnectionDeadline
from .request_target import split_path_query
from .recipient import (CONNECTION_MUST_CLOSE, CONNECTION_NEEDS_DRAIN,
                        AbstractReader, HTTP1Recipient, IncompleteReadError,
                        ReadLimitExceeded, RecipientFactory, _HEAD_END,
                        _WS_READ_INLINE)
from .sender import AbstractWriter, SenderFactory
from .access_log import (AccessLogRecord as _AccessLogRecord,
                         _make_disconnect_detecting_receive,
                         close_ws_record as _close_ws_record,
                         disconnect_events_observed as _disconnect_events_observed,
                         emit_access_log as _emit_access_log,
                         request_record_needed as _request_record_needed,
                         start_record as _start_record,
                         PHASE_TRACE as _PHASE_TRACE)
from .cap_log import log_cap_hit

logger = logging.getLogger(__name__)

_WS_GUID = b'258EAFA5-E914-47DA-95CA-C5AB0DC85B11'  # RFC 6455 §1.3
# RFC 9112 §3.2.4 — the methods a server-wide ``OPTIONS *`` advertises.
_SERVER_WIDE_ALLOW = b'GET, HEAD, POST, PUT, PATCH, DELETE, OPTIONS'
_HTTP_PORT  = 80
_HTTPS_PORT = 443

_MAX_KEEPALIVE_DRAIN = 64 * 1024

# TLS connections do NOT advertise pathsend: ``loop.sendfile`` raises
# NotImplementedError on SSL transports (the kernel can't see the plaintext).
_H1_PATHSEND_EXTENSIONS = {'http.response.pathsend': {}}

# RFC 9112 §4 — HTTP-version = "HTTP/" DIGIT "." DIGIT
_HTTP_VERSION_RE = re.compile(rb'\AHTTP/\d\.\d\Z')

# Share field octet grammar with HTTP/2.
from ..protocol.field_grammar import (
    COMMON_METHODS_OCTETS, FIELD_VALUE_ALLOWED_OCTETS, URI_SCHEME_RE,
    FieldError, field_line, field_value, host_field_value,
    method_token_is_valid)



def _block_values_are_clean(data: bytes) -> bool:
    """True when no field value in *data* can contain a forbidden octet.

    A ``False`` result never rejects and says nothing about *which* line is at
    fault — it only turns the per-value check back on, so error messages are
    unchanged.
    """
    residue = data.translate(None, FIELD_VALUE_ALLOWED_OCTETS)
    return residue.count(b'\r\n') * 2 == len(residue)


# Bound attacker-controlled cache keys by bytes, per connection;
# an entry-count limit alone does not bound retained memory.

#: Entries.
_LINE_CACHE_MAX = 64

_LINE_CACHE_MAX_LINE = 1024

# Total retained key bytes per connection; value slices also consume memory.
_LINE_CACHE_MAX_BYTES = 8192


# Admit only explicitly allowed fixed lines to the shared table; exclude framing headers.
_SPEC_ENUMERATED_LINES: tuple[bytes, ...] = (
    # Fetch Metadata Request Headers (W3C) — closed value sets.
    *(b'Sec-Fetch-Site: ' + v for v in
      (b'cross-site', b'same-origin', b'same-site', b'none')),
    *(b'Sec-Fetch-Mode: ' + v for v in
      (b'cors', b'navigate', b'no-cors', b'same-origin', b'websocket')),
    *(b'Sec-Fetch-Dest: ' + v for v in
      (b'audio', b'audioworklet', b'document', b'embed', b'empty', b'font',
       b'frame', b'iframe', b'image', b'manifest', b'object', b'paintworklet',
       b'report', b'script', b'serviceworker', b'sharedworker', b'style',
       b'track', b'video', b'worker', b'xslt')),
    b'Sec-Fetch-User: ?1',
    # UA client hints (RFC 8942 / W3C) — booleans and the platform enum.
    b'sec-ch-ua-mobile: ?0',
    b'sec-ch-ua-mobile: ?1',
    *(b'sec-ch-ua-platform: "' + v + b'"' for v in
      (b'Android', b'Chrome OS', b'Chromium OS', b'iOS', b'Linux', b'macOS',
       b'Windows', b'Unknown')),
    # Fixed tokens (RFC 9110 / RFC 9112 / RFC 6797 / DNT).
    b'Upgrade-Insecure-Requests: 1',
    b'DNT: 0',
    b'DNT: 1',
    b'TE: trailers',
    b'Pragma: no-cache',
    b'Connection: keep-alive',
    b'Connection: Keep-Alive',
    b'Connection: close',
    *(b'Cache-Control: ' + v for v in
      (b'no-cache', b'no-store', b'max-age=0')),
    # Universal idioms, not spec enums — the one exception to the rule above,
    # and few on purpose.
    b'Accept: */*',
    b'Accept-Encoding: gzip',
    b'Accept-Encoding: gzip, deflate',
    b'Accept-Encoding: gzip, deflate, br',
    b'Accept-Encoding: gzip, deflate, br, zstd',
    b'Accept-Encoding: identity',
)


def _build_default_lines() -> dict[bytes, tuple[bytes, bytes]]:
    """Map every default line to the pair ``_parse`` produces for it.

    Lines go through the same ``field_line``/``field_value`` as parsing; a
    violation raises ``FieldError``/``ValueError`` at import.
    """
    table: dict[bytes, tuple[bytes, bytes]] = {}
    for line in _SPEC_ENUMERATED_LINES:
        lkey, raw = field_line(line)
        if lkey in _UNDERSCORE_FRAMING_NAMES or lkey in _FRAMING_NAMES:
            raise ValueError(
                f'framing header must not be pre-seeded: {line!r}')
        value = field_value(raw)
        if len(line) > _LINE_CACHE_MAX_LINE:
            raise ValueError(f'default header line too long: {line!r}')
        table[line] = (lkey, value)
    return table


#: Names a shared table must never carry — a framing decision is read from the
#: request every time.
_FRAMING_NAMES = frozenset({
    b'content-length', b'transfer-encoding', b'host', b'expect', b'upgrade',
    b'trailer', b'te-framing',
})

# Validate raw Content-Length before stripping OWS: accept one leading SP
# and canonical decimal only. Reject leading zeros, tabs and trailing whitespace.
_CL_STRICT_RE = re.compile(rb'\A ?(?:0|[1-9][0-9]*)\Z')

# Reject underscore spellings of framing headers to avoid proxy normalization ambiguity.
_UNDERSCORE_FRAMING_NAMES = frozenset((
    b'content_length', b'transfer_encoding'))

_DEFAULT_LINES = _build_default_lines()


class BadRequestError(Exception):
    """Raised by ``HTTP1Actor._parse`` on an RFC 9112 framing violation.

    The actor's keep-alive loop catches this and sends a 400 Bad Request
    before closing the connection — never tries to dispatch the malformed
    request to the app.
    """


class HeaderTooLargeError(Exception):
    """Raised when a request header line or the whole header block exceeds
    the configured limit (``BB_HEADER_MAX_LINE`` / ``BB_HEADER_MAX_TOTAL``).

    The actor answers with 431 Request Header Fields Too Large (RFC 6585
    §5) and closes the connection.  Distinct from [`BadRequestError`][]
    because the response status differs.
    """


class NotImplementedFramingError(Exception):
    """RFC 9112 §6.1 — the request used a Transfer-Encoding the server
    does not implement.  Answered with 501 Not Implemented (a separate
    response code from [`BadRequestError`][]'s 400)."""


class UnsupportedVersionError(Exception):
    """RFC 9110 §15.6.6 — the request-line carried a well-formed
    ``HTTP/x.y`` version whose major version the server does not support
    (RFC9112-2.3-INVALID-VERSION).  Answered with 505 HTTP Version Not
    Supported and the connection is closed.  Distinct from
    [`BadRequestError`][]: the request *grammar* was valid."""


def _reject_oversized_head(head: bytes, max_total: int) -> None:
    """Raise the right rejection for a head that overran the total budget.

    Always raises: **400** when the first CRLF lands beyond the budget, because
    the start-line never ended (RFC 9112 §3) and a 100 KiB method token is not
    "too many header fields"; **431** when the lines are well-formed and merely
    numerous (RFC 6585 §5).
    """
    first_eol = head.find(b'\r\n')
    if first_eol < 0 or first_eol >= max_total:
        raise BadRequestError(
            f'request line exceeds BB_HEADER_MAX_TOTAL={max_total} '
            f'without a line terminator')
    raise HeaderTooLargeError(
        f'header block {len(head)} bytes > BB_HEADER_MAX_TOTAL={max_total}')


def _declares_content(headers: 'Headers') -> bool:
    """True if the request's framing headers announce a non-empty body.

    Answers "are there body octets on this connection?", which is a different
    question from ``HTTP1Recipient.needs_drain()`` ("are unread body octets
    still buffered?"): this one is asked *before* a recipient exists, on the
    upgrade path, where the answer decides whether to switch protocols at all.

    Assumes [`_validate_message_framing`][] has already run, so CL/TE
    conflicts and malformed values cannot reach here — a bare ``chunked`` or a
    single well-formed ``Content-Length`` is all that is left to classify.
    """
    if headers.getlist(b'transfer-encoding'):
        return True
    cl = headers.get(b'content-length', b'')
    # ``Content-Length: 0`` — and ``000`` — declares no octets, so there is
    # nothing that could be framed two ways.
    return bool(cl) and bool(cl.lstrip(b'0'))


def _validate_message_framing(cls: list | None, tes: list | None) -> int:
    """Validate framing once and return declared Content-Length, or zero.

    Reject CL+TE, malformed lists, conflicting lengths, and non-final or doubled
    chunked with 400. Only bare chunked is implemented: other codings or
    parameters raise NotImplementedFramingError (501). The shared split retains
    parameters, so parametered chunked still counts when grading order and
    multiplicity. Chunked declares no total; count its body while receiving.
    """
    if cls and tes:
        raise BadRequestError(
            'Content-Length and Transfer-Encoding both present '
            '(smuggling vector)')

    declared = 0
    if cls:
        try:
            declared = parse_content_length(cls) or 0
        except ValueError as exc:
            raise BadRequestError(str(exc)) from exc

    if tes:
        try:
            members = split_transfer_codings(tes)
        except ValueError as exc:
            # A list no reading can split (``gzip;bad``, a field of commas)
            # declares a length no one can determine.
            raise BadRequestError(str(exc)) from exc
        if members == [(b'chunked', ())]:
            return declared  # RFC 9112 §6.1 — the one accepted form
        codings = [name for name, _params in members]
        if b'chunked' not in codings:
            # A coding we don't implement, chunked absent (``gzip``,
            # ``deflate``) ⇒ 501 (nginx parity).
            raise NotImplementedFramingError(
                f'Transfer-Encoding {codings!r} is not implemented')
        if codings[-1] != b'chunked' or codings.count(b'chunked') > 1:
            # chunked present but not the sole final coding (``chunked, gzip``,
            # ``chunked, chunked``; a parametered ``chunked`` counts too) ⇒ the
            # length is undeterminable, and a server MUST NOT process the
            # message (SMUG-TE-NOT-FINAL-CHUNKED).
            raise BadRequestError(
                f'Transfer-Encoding with chunked not the sole final coding: '
                f'{codings!r}')
        # chunked IS final and sole but the list is still not the one accepted
        # form: a coding precedes it (``gzip, chunked``) or a parameter sits on
        # it (``chunked; ext=1``, RFC 9112 §7.1) ⇒ 501.
        raise NotImplementedFramingError(
            f'Transfer-Encoding {members!r} applies an unimplemented '
            f'coding or parameter with chunked')

    return declared


# RFC 9112 §2.1 / RFC 3986 — a request-target may carry only visible ASCII.
_TARGET_ALLOWED_OCTETS = bytes(range(0x21, 0x7F))


def _parse_host_header(value: bytes, default_port: int) -> tuple[str, int]:
    """Split a Host header value into ``(host, port)``.

    Handles the RFC 3986 §3.2.2 IPv6 bracket form ``[::1]:8100``, where a naive
    ``value.split(b':')`` yields ``int(b'')`` → ``ValueError``.  A missing or
    non-numeric port falls back to *default_port*; the reg-name path cuts the
    host at the first ``:`` (§3.2.2 — a reg-name carries none), so no port text
    survives in the host.
    """
    # ``host_field_value`` rejects non-ASCII on the request path; ``replace``
    # keeps this total for every other caller.
    def _dec(b: bytes) -> str:
        return b.decode('utf-8', errors='replace')

    if value.startswith(b'['):
        end = value.find(b']')
        if end != -1:
            host = value[1:end]
            rest = value[end + 1:]
            if rest.startswith(b':') and rest[1:].isdigit():
                return _dec(host), int(rest[1:])
            return _dec(host), default_port
        # Unterminated bracket — treat the whole value as the host.
        return _dec(value), default_port
    host, sep, port_s = value.partition(b':')
    if sep and port_s.isdigit():
        return _dec(host), int(port_s)
    return _dec(host), default_port


# ---------------------------------------------------------------------------
# RequestActor — single HTTP request lifetime
# ---------------------------------------------------------------------------

class RequestActor(Actor):
    """Owns one request's app boundary: what the app is called with.

    Shared by both protocol actors — H/1 reuses one instance per connection
    via [`bind`][]; H/2 builds one per stream.  Owns the app-facing
    representation (the native [`Connection`][] on the default lane, the
    materialized ASGI scope on ``BB_FORCE_ASGI_SCOPE=1``), binds the raw
    recipient before any wrapper exists, and calls the app.

    The request-lifecycle Level B events are emitted by the application
    layer (``BlackBull._dispatch`` / ``__call__``) —
    the cross-transport emission points — not here.  The
    actor layer emits only the Level B ``error`` event, for exceptions that
    escape the app call (e.g. a raising global middleware).
    """

    def __init__(
        self,
        conn: Connection,
        recipient: ASGIReceiveCallable,
        send: ASGISendCallable,
        app: Callable[..., Awaitable[None]],
        # ``None`` for a foreign ASGI app: the actor skips event emission
        # rather than the caller forking to a second dispatch path.
        aggregator: EventAggregator | None,
        force_asgi: bool,
    ) -> None:
        super().__init__()
        self._conn = conn
        self._recipient = recipient
        self._send = send
        self._app = app
        self._aggregator = aggregator
        self._force_asgi = force_asgi

    def bind(self, conn: Connection, recipient: ASGIReceiveCallable,
             send: ASGISendCallable) -> 'RequestActor':
        """Bind the next sequential HTTP/1.1 request; retain per-connection settings.

        Do not share this actor across concurrent HTTP/2 streams.
        """
        self._conn = conn
        self._recipient = recipient
        self._send = send
        return self

    async def run(self) -> None:  # override: single-shot, no inbox loop
        # Keep the common validation branch inline on the per-request path.
        if self._force_asgi:
            target = self._conn.to_asgi_scope(force_asgi=True)
        else:
            target = self._conn
        # Bind the raw recipient before wrappers to avoid a target/wrapper reference cycle.
        bind_receive_channel(target, self._recipient)
        # Only when a listener observes it — otherwise the raw recipient, and
        # no per-request closure.  Body-level disconnect (``target.body()`` →
        # ClientDisconnected) is independent of this wrapper.
        if (self._aggregator is not None
                and _disconnect_events_observed(self._aggregator)):
            receive = _make_disconnect_detecting_receive(
                self._recipient, target, self._aggregator)
        else:
            receive = self._recipient
        try:
            await self._app(target, receive, self._send)
        except BaseException as e:
            if self._aggregator is not None:
                await self._aggregator.on_error(target, e)
            raise

    async def _handle(self, msg: Message) -> None:  # never reached
        raise NotImplementedError


# ---------------------------------------------------------------------------
# HTTP1Actor — keep-alive connection loop
# ---------------------------------------------------------------------------

class HTTP1Actor(Actor):
    """Drives the HTTP/1.1 keep-alive loop for one connection.

    Supervisor strategy: isolate — an unhandled exception from a RequestActor
    closes the connection without crashing sibling connections.

    If *aggregator* is ``None`` the actor fires events through
    ``app._dispatcher`` directly, so an app assembled without an
    EventAggregator still receives the lifecycle events.
    """

    # Class-level defaults, because test doubles built with ``object.__new__``
    # never run ``__init__``.  Immutable ones only: a mutable class default is
    # shared by *every* connection, which for the line cache is precisely the
    # cross-connection bleed its design forbids.
    _ssl: bool = False
    _line_cache: 'dict[bytes, tuple[bytes, bytes]] | None' = None
    _line_cache_bytes: int = 0
    #: Body length the current request declares, as validated by
    #: [`_validate_message_framing`][]; 0 when it declares none.
    _declared_body_len: int = 0
    #: ``(content_length, chunked)`` of the request ``_parse`` validated last.
    _request_framing: tuple[int | None, bool] = (None, False)
    _expects_continue: bool = False

    def __init__(
        self,
        reader: AbstractReader,
        writer: AbstractWriter,
        app: Callable[..., Awaitable[None]],
        aggregator: 'EventAggregator | None',
        *,
        request: bytes = b'',
        peername: tuple[str, int | None] | None = None,
        sockname: tuple[str, int | None] | None = None,
        ssl: bool = False,
        ws_queue_depth: int = _WS_READ_INLINE,
        deadline: ConnectionDeadline | None = None,
        connection_id: str = '',
    ) -> None:
        super().__init__()
        # Bytes that already arrived go back in front of the stream, not into a
        # parsed-head slot: the actor then has exactly one way to obtain a head,
        # and a partial one still gets the budget check and the framing rules.
        if request:
            from .recipient import PrefixReader  # noqa: PLC0415
            reader = PrefixReader(request, reader)
        self._reader = reader
        self._writer = writer
        self._app = app
        self._aggregator = aggregator
        self._request = b''
        self._peername = peername
        self._sockname = sockname
        self._ssl = ssl
        self._ws_queue_depth = ws_queue_depth
        self._request_actor: RequestActor | None = None
        self._connection_id = connection_id
        self._deadline = deadline

    async def run(self) -> None:
        """Keep-alive loop — process requests until connection closes."""
        cfg = get_settings()
        driven_without_connection_actor = self._deadline is None
        if driven_without_connection_actor:
            self._deadline = ConnectionDeadline()
        dl = self._deadline
        max_body_size = cfg.max_body_size
        send = SenderFactory.http1(self._writer)
        # Quantifies the between-request gap (dispatch_done(N) →
        # loop_start(N+1)) on a keep-alive connection.
        _loop_start_perf: float = 0.0
        _loop_start_cpu: float = 0.0
        inner_receive: HTTP1Recipient | None = None
        keep_alive = False
        try:
            while True:
                if _PHASE_TRACE:
                    _loop_start_perf = _time.perf_counter()
                    _loop_start_cpu = _time.process_time()
                # Slowloris defence (RFC 9110 §15.5.9 — 408 Request Timeout).
                # ``header_timeout=0`` disables the deadline — only sound for
                # trusted local clients.
                idle_window = (cfg.keep_alive_timeout if keep_alive
                               else cfg.header_timeout)
                try:
                    if idle_window > 0:
                        with dl.guard(idle_window):
                            await self._read_headers(cfg.header_max_total)
                    else:
                        await self._read_headers(cfg.header_max_total)
                except IncompleteReadError:
                    partial_head_before_eof = bool(self._request)
                    if partial_head_before_eof:
                        # 400 before close, so the differential tests read a
                        # protocol violation rather than a reset.
                        logger.info(
                            '400 Bad Request — peer EOF mid-headers '
                            'after %d bytes; peer=%r',
                            len(self._request), self._peername,
                        )
                        await self._send_error_and_close(
                            send, b'400 Bad Request', HTTPStatus.BAD_REQUEST)
                    return
                except HeaderTooLargeError as exc:
                    # RFC 6585 §5.  Close so an attacker cannot keep feeding us
                    # bytes after the reply.
                    logger.warning('431 Request Header Fields Too Large: %s', exc)
                    log_cap_hit('header_max_total',
                                requested=len(self._request),
                                limit=cfg.header_max_total,
                                peer=self._peername, protocol='http1')
                    await self._send_error_and_close(
                        send, b'431 Request Header Fields Too Large',
                        HTTPStatus.REQUEST_HEADER_FIELDS_TOO_LARGE)
                    return
                except BadRequestError as exc:
                    # The other half of the budget verdict (RFC 9112 §3), and
                    # caught here as well as around ``_parse`` because the head
                    # read is the first place that can reach it.
                    logger.warning('400 Bad Request: %s', exc)
                    await self._send_error_and_close(
                        send, b'400 Bad Request', HTTPStatus.BAD_REQUEST)
                    return
                except (asyncio.TimeoutError, TimeoutError):
                    if keep_alive:
                        # No status: the previous response already shipped, and
                        # either side may close a persistent connection at any
                        # time (RFC 9112 §9.3).  A 408 would arrive on a
                        # connection the peer has most likely abandoned.
                        return
                    logger.warning(
                        '408 Request Timeout (slowloris defence) — peer=%r '
                        'sent %d bytes in %.1fs without completing headers',
                        self._peername, len(self._request), cfg.header_timeout)
                    log_cap_hit('header_timeout',
                                requested=cfg.header_timeout,
                                limit=cfg.header_timeout,
                                peer=self._peername, protocol='http1')
                    await self._send_error_and_close(
                        send, b'408 Request Timeout', HTTPStatus.REQUEST_TIMEOUT)
                    return

                if keep_alive and self._request == _HEAD_END:
                    # Trailing CRLFs from a client that appends them to a body.
                    # RFC 9112 §2.2 says tolerate empty lines before a
                    # request-line — and none is coming.  Not an error.
                    return

                try:
                    conn = self._parse(self._request)
                    # RFC 9110 §15.2 gives a 1xx to HTTP/1.1 clients only.
                    send.supports_interim = conn.http_version != '1.0'
                except HeaderTooLargeError as exc:
                    # The per-line limit, hit during parse.
                    logger.warning('431 Request Header Fields Too Large: %s', exc)
                    log_cap_hit('header_max_line',
                                requested=len(self._request),
                                limit=cfg.header_max_line,
                                peer=self._peername, protocol='http1')
                    await self._send_error_and_close(
                        send, b'431 Request Header Fields Too Large',
                        HTTPStatus.REQUEST_HEADER_FIELDS_TOO_LARGE)
                    return
                except BadRequestError as exc:
                    # RFC 9112 §3 / §5.  A malformed request is a smuggling
                    # vector candidate, so the connection always terminates
                    # rather than hunting for the next message boundary.
                    logger.warning('400 Bad Request: %s', exc)
                    await self._send_error_and_close(
                        send, b'400 Bad Request', HTTPStatus.BAD_REQUEST)
                    return
                except NotImplementedFramingError as exc:
                    # RFC 9112 §6.1 — nginx parity: 501, then close.
                    logger.warning('501 Not Implemented: %s', exc)
                    await self._send_error_and_close(
                        send, b'501 Not Implemented', HTTPStatus.NOT_IMPLEMENTED)
                    return
                except UnsupportedVersionError as exc:
                    logger.warning('505 HTTP Version Not Supported: %s', exc)
                    await self._send_error_and_close(
                        send, b'505 HTTP Version Not Supported',
                        HTTPStatus.HTTP_VERSION_NOT_SUPPORTED)
                    return
                self._fill_connection_info(conn)

                if conn.type == 'websocket':
                    await self._handle_upgrade(conn)
                    return

                # Refuse oversized declared bodies before Expect: 100-continue. Close without
                # reusing framing positioned before the unread body (RFC 9110 §10.1.1, §15.5.14).
                oversized = self._declared_body_len
                if max_body_size and oversized > max_body_size:
                    logger.warning(
                        '413 Content Too Large — %s %s declares %d bytes, '
                        'BB_MAX_BODY_SIZE=%d; peer=%r',
                        conn.method, conn.path, oversized, max_body_size,
                        self._peername)
                    log_cap_hit('max_body_size',
                                requested=oversized,
                                limit=max_body_size,
                                peer=self._peername,
                                scope_path=conn.path, protocol='http1')
                    await self._send_error_and_close(
                        send, b'413 Content Too Large',
                        HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
                    return

                # BB_REQUEST_TIMEOUT, parity with ``HTTP2Actor``'s per-stream
                # ``asyncio.wait_for``.  No keep-alive across a timed-out
                # request.
                try:
                    if cfg.request_timeout > 0:
                        ok, inner_receive = await asyncio.wait_for(
                            self._dispatch_request(
                                conn, send, cfg, dl, inner_receive,
                                _loop_start_perf, _loop_start_cpu),
                            timeout=cfg.request_timeout,
                        )
                    else:
                        ok, inner_receive = await self._dispatch_request(
                            conn, send, cfg, dl, inner_receive,
                            _loop_start_perf, _loop_start_cpu)
                except (asyncio.TimeoutError, TimeoutError):
                    logger.warning(
                        '408 Request Timeout — handler on %s %s exceeded '
                        'BB_REQUEST_TIMEOUT=%.1fs; closing connection',
                        conn.method, conn.path,
                        cfg.request_timeout,
                    )
                    log_cap_hit('request_timeout',
                                requested=cfg.request_timeout,
                                limit=cfg.request_timeout,
                                peer=self._peername,
                                scope_path=conn.path,
                                protocol='http1')
                    if not send._started:
                        await self._send_error_and_close(
                            send, b'408 Request Timeout', HTTPStatus.REQUEST_TIMEOUT)
                    break
                if not ok:
                    break  # unhandled error — close connection

                # Every keep-alive exit uses the recipient drain verdict; see the drain invariant.
                verdict = inner_receive.after_dispatch()
                if verdict is CONNECTION_MUST_CLOSE:
                    break
                if (verdict is CONNECTION_NEEDS_DRAIN
                        and not await inner_receive.drain(_MAX_KEEPALIVE_DRAIN)):
                    break

                # RFC 9112 §9.1 — honour Connection: close.
                if not self._should_keep_alive(conn):
                    break

                self._request = b''
                keep_alive = True

        except IncompleteReadError:
            # Safety net for an IncompleteReadError that escaped the explicit
            # catches above (a body-read EOF ``HTTP1Recipient`` did not
            # absorb).  The peer is gone: drop what is in flight rather than
            # raise on a dead pipe.
            send.mark_client_gone()

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    @staticmethod
    async def _send_error_and_close(send, body: bytes, status: HTTPStatus) -> None:
        """Send a plain-text error response with ``Connection: close``."""
        await send(
            body, status,
            [(b'connection', b'close'), (b'content-type', b'text/plain')],
        )

    def _parse(self, data: bytes) -> Connection:
        """Parse raw HTTP/1.1 request bytes into a native [`Connection`][].

        The Connection's header names are lowercase tchar, its values carry no
        edge SP/HTAB and no CTL, and ``host`` is at most one valid authority.
        Raises [`BadRequestError`][] on an RFC 9112 framing violation the
        caller should answer with 400, and [`HeaderTooLargeError`][] when a
        single line exceeds ``BB_HEADER_MAX_LINE``.  The whole-block limit
        (``BB_HEADER_MAX_TOTAL``) is enforced in ``run()``, which sees the
        accumulating buffer; per-line is cheaper here, post-split.
        """
        max_line = get_settings().header_max_line
        lines = data.split(b'\r\n')
        # No line can be longer than the block that contains it, so the
        # per-line walk is only reachable for a block that is itself over the
        # limit — one comparison retires it for every request under 8 KiB.
        if max_line > 0 and len(data) > max_line:
            for ln in lines:
                if len(ln) > max_line:
                    raise HeaderTooLargeError(
                        f'header line {len(ln)} bytes > BB_HEADER_MAX_LINE={max_line}')

        # RFC 9112 §2.2 — recipients MAY skip a stray empty line before the
        # request.  We tolerate one to be polite to HTTP/1.0 clients.
        idx = 0
        if lines and lines[0] == b'':
            idx = 1
        if idx >= len(lines):
            raise BadRequestError('empty request')

        request_line = lines[idx]
        parts = request_line.split(b' ')
        if len(parts) != 3:
            raise BadRequestError(
                f'request line must have exactly 3 SP-separated parts, '
                f'got {len(parts)}: {request_line!r}')
        method, path, version = parts

        # Method (§4 / RFC 9110 §9.1) — the same rule HTTP/2 grades `:method`
        # with, so the transports cannot disagree about which methods exist.
        # A common method skips the rule: the set lies inside it, which
        # tests/architecture pins.
        if (method not in COMMON_METHODS_OCTETS
                and not method_token_is_valid(method)):
            raise BadRequestError(f'invalid method {method!r}')

        # HTTP-version (§2.5) — exactly ``HTTP/d.d``.
        if not _HTTP_VERSION_RE.match(version):
            raise BadRequestError(f'invalid HTTP-version {version!r}')
        # RFC 9110 §2.5 / §15.6.6 (RFC9112-2.3-INVALID-VERSION) — a higher 1.x
        # minor (``HTTP/1.2``) is 1.x-compatible and served as 1.1; any other
        # major → 505.  The HTTP/2 preface never reaches this parser:
        # ``protocol_registry`` sniffs ``PRI * HTTP/2.0`` off the socket before
        # the HTTP/1.1 binding is selected.
        if version[5:6] != b'1':
            raise UnsupportedVersionError(
                f'HTTP version {version!r} is not supported')

        # Request-target form dispatch (RFC 9112 §3.2).  Four forms:
        # origin (``/path``), absolute (``http://host/path``), authority
        # (CONNECT only), and asterisk (``*``, server-wide OPTIONS).
        authority_override: bytes | None = None
        asterisk_form = False
        if method_is(method, 'CONNECT'):
            # authority-form target (§3.2.3) — tunnel establishment, which
            # BlackBull does not implement.  Answer 501, not a spurious 404.
            raise NotImplementedFramingError(
                f'CONNECT (tunneling) is not implemented: {path!r}')

        if path == b'*':
            # asterisk-form (§3.2.4) — a server-wide request, valid only for
            # OPTIONS.
            if method != b'OPTIONS':
                raise BadRequestError(
                    f'asterisk-form request-target is valid only for OPTIONS, '
                    f'not {method!r}')
            asterisk_form = True
        else:
            # Request-target octets — reject CTLs, DEL, and non-ASCII (§2.1 /
            # RFC 3986; MAL-NON-ASCII-URL).  Before the absolute-form rewrite
            # below, whose scheme and authority would otherwise reach the host
            # header with no per-value check.
            if not path or path.translate(None, _TARGET_ALLOWED_OCTETS):
                raise BadRequestError(f'invalid request-target {path!r}')
            if (_ss := path.find(b'://')) != -1 and b'/' not in path[:_ss]:
                # absolute-form (§3.2.2): ``scheme "://" authority
                # path-abempty``.  Rewrite it to origin-form and let the
                # authority override Host (§3.2.2 — the origin server MUST
                # ignore the Host header here).
                # RFC 3986 §3.1 — the target's scheme grammar.  conn.scheme
                # stays the connection's: the target must not claim https.
                if not URI_SCHEME_RE.fullmatch(path[:_ss]):
                    raise BadRequestError(
                        f'invalid scheme in absolute-form request-target: '
                        f'{path[:_ss]!r}')
                rest = path[_ss + 3:]
                authority_end = len(rest)
                for delimiter in (b'/', b'?', b'#'):
                    offset = rest.find(delimiter)
                    if offset != -1 and offset < authority_end:
                        authority_end = offset
                if authority_end == len(rest):
                    authority_override, path = rest, b'/'
                else:
                    authority_override, path = rest[:authority_end], rest[authority_end:]
                    if not path.startswith(b'/'):
                        path = b'/' + path
                if not authority_override:
                    raise BadRequestError(
                        f'absolute-form request-target has empty authority: '
                        f'{path!r}')

        if asterisk_form:
            _decoded_path, _raw_path_b, _query_string = '*', b'*', b''
        else:
            _decoded_path, _raw_path_b, _query_string = split_path_query(path)

        values_need_checking = not _block_values_are_clean(data)

        cache = self._line_cache
        if cache is None:
            cache = self._line_cache = {}
        do_lookup = len(cache) > 0

        raw: list[tuple[bytes, bytes]] = []
        index: dict[bytes, list[tuple[bytes, bytes]]] = {}
        for line in lines[idx + 1:]:
            if not line:
                # Empty line = end of headers; anything after is body (already
                # split off upstream because we read until CRLFCRLF).
                continue
            # Only probe lines eligible for cache admission.
            cacheable = len(line) <= _LINE_CACHE_MAX_LINE
            if cacheable:
                # This peer's own lines first, then the shared spec table.
                hit = cache.get(line) if do_lookup else None
                if hit is None:
                    hit = _DEFAULT_LINES.get(line)
                if hit is not None:
                    # Safe even when ``values_need_checking`` is True for
                    # *this* block: the hit was proved clean when it was
                    # admitted, and its bytes have not changed since.
                    raw.append(hit)
                    same = index.get(hit[0])
                    if same is None:
                        index[hit[0]] = [hit]
                    else:
                        same.append(hit)
                    continue
            try:
                lkey, value = field_line(line)
                if lkey in _UNDERSCORE_FRAMING_NAMES:
                    raise FieldError(f'framing-confusable header name {lkey!r} '
                                     f'(NORM-UNDERSCORE)')
                if lkey == b'content-length' and not _CL_STRICT_RE.match(value):
                    raise FieldError(f'ambiguous Content-Length value {value!r} '
                                     f'(RFC 9110 §8.6)')
                value = field_value(value, check=values_need_checking)
            except FieldError as exc:
                raise BadRequestError(str(exc)) from None
            pair = (lkey, value)
            raw.append(pair)
            same = index.get(lkey)
            if same is None:
                index[lkey] = [pair]
            else:
                same.append(pair)
            # Admission last, and tested against the *resulting* byte total, so
            # the budget is a ceiling no final line can step over.
            if (cacheable
                    and len(cache) < _LINE_CACHE_MAX
                    and self._line_cache_bytes + len(line) <= _LINE_CACHE_MAX_BYTES):
                cache[line] = pair
                self._line_cache_bytes += len(line)

        # RFC 9112 §3.2.2 — the request's own authority is definitive, so a
        # spoofed Host cannot influence routing or host validation
        # (SMUG-ABSOLUTE-URI-HOST-MISMATCH).
        if authority_override is not None:
            raw = [(k, v) for k, v in raw if k != b'host']
            raw.append((b'host', authority_override))
            headers = Headers.from_lowered(raw)
            index = headers._index
        else:
            # The loop above lowercased each name and indexed it.
            headers = Headers._adopt(raw, index)

        # RFC 9110 §8.3 — Content-Type is a singleton; multiple values are
        # ambiguous and a request-smuggling surface (COMP-DUPLICATE-CT).
        content_types = index.get(b'content-type')
        if content_types is not None and len(content_types) > 1:
            raise BadRequestError('multiple Content-Type headers')

        # RFC 9112 §6 — framing rejected before any body byte is read.  ``run``
        # weighs the returned length against ``BB_MAX_BODY_SIZE``.
        content_length = index.get(b'content-length')
        transfer_encoding = index.get(b'transfer-encoding')
        self._declared_body_len = _validate_message_framing(
            content_length, transfer_encoding)
        self._request_framing = (
            self._declared_body_len if content_length else None,
            transfer_encoding is not None)
        expect = index.get(b'expect')
        self._expects_continue = (
            expect is not None and expect[0][1].lower() == b'100-continue')
        try:
            host_value = host_field_value(index.get(b'host'))
        except FieldError as exc:
            raise BadRequestError(str(exc)) from None
        # RFC 9112 §3.2 / §7.2 — every HTTP/1.1 (and later 1.x) request MUST
        # carry a Host header (RFC9112-7.1-MISSING-HOST); only HTTP/1.0, which
        # predates Host, may omit it (COMP-HTTP10-NO-HOST).
        if version != b'HTTP/1.0' and host_value is None:
            raise BadRequestError(
                f'missing Host header on {version.decode("ascii")} request '
                f'(RFC 9112 §3.2)')

        conn = Connection(
            type='http',
            http_version=version[5:].decode('utf-8'),
            method=method.decode('utf-8'),
            scheme='http',
            path=_decoded_path,
            # ASGI: raw_path is the undecoded path component only — the
            # query string is carried in query_string, never here.
            raw_path=_raw_path_b,
            query_string=_query_string,
            # NOT taken from the client-controlled ``X-Forwarded-Prefix`` — a
            # client could spoof the mount point.  Only the ``TrustedProxy``
            # middleware sets it, after verifying the peer.
            root_path='',
            headers=headers,
            client=None,
            server=None,
            extensions=_H1_PATHSEND_EXTENSIONS if not self._ssl else {},
        )

        if asterisk_form:
            conn._asterisk_form = True

        if host_value is not None:
            default_port = _HTTPS_PORT if self._ssl else _HTTP_PORT
            host, port = _parse_host_header(host_value, default_port)
            conn.server = (host, port)

        upgrade = index.get(b'upgrade')
        if upgrade:
            # RFC 9110 §7.8 — a server MAY ignore an Upgrade it does not
            # support and MUST NOT fail the request over it.  Only WebSocket
            # may switch ``conn.type``; any other token (notably curl's default
            # ``Upgrade: h2c`` probe on ``--http2``) is served as ordinary
            # HTTP/1.1, because dispatch has no route for it and the connection
            # would close with no reply.
            if upgrade[0][1].lower() == b'websocket':
                conn.type = 'websocket'
                conn.scheme = 'ws'

        return conn

    def _fill_connection_info(self, conn: Connection) -> None:
        if self._peername is not None:
            conn.client = tuple(self._peername)

        if conn.server is None and self._sockname is not None:
            conn.server = tuple(self._sockname)

        if self._ssl:
            conn.scheme = 'wss' if conn.type == 'websocket' else 'https'

    async def _handle_upgrade(self, conn: Connection) -> None:
        """Handle WebSocket upgrade, threading the native Connection."""
        from .conn_id import new_connection_id  # noqa: PLC0415
        from .websocket_actor import WebSocketActor  # noqa: PLC0415
        aggregator = self._aggregator
        if aggregator is None:
            # No aggregator — use a silent dispatcher so WebSocketActor can fire
            # lifecycle events without any subscribers receiving them.
            from ..event import EventDispatcher  # noqa: PLC0415
            from ..event_aggregator import EventAggregator  # noqa: PLC0415
            aggregator = EventAggregator(EventDispatcher())

        log_record = _start_record(conn)
        log_record.status = 101

        if not await self._do_ws_handshake(conn):
            return  # 400 already sent
        # One id per TCP connection: reuse the accept-time id; mint one only
        # when the actor was constructed without it (direct test drives).
        conn.connection_id = self._connection_id or new_connection_id()
        ws_actor = WebSocketActor(
            self._reader, self._writer, conn, self._app, aggregator,
            peername=self._peername, sockname=self._sockname, ssl=self._ssl,
            ws_queue_depth=self._ws_queue_depth,
        )
        try:
            await ws_actor.run()
        finally:
            _close_ws_record(log_record, ws_actor._disconnect_code)

    async def _do_ws_handshake(self, conn: Connection) -> bool:
        """Validate the WebSocket upgrade and store a deferred 101 callback.

        Returns True if the handshake is valid and ready to proceed, False if
        a 400 Bad Request was already sent (declared content, bad
        Sec-WebSocket-Key, or bad Sec-WebSocket-Version).

        The actual HTTP 101 response is deferred: it is sent by
        WebSocketActor._send when the ASGI app calls websocket.accept, so that
        the chosen subprotocol from that event can be included in the 101 headers
        (RFC 6455 §4.2.2).
        """
        send = SenderFactory.http1(self._writer)
        headers = conn.headers
        # RFC 9110 §9.3.1 — refuse rather than drain; see
        # ``docs/about/internals.md`` §Keep-alive drain invariant.
        if _declares_content(headers):
            logger.warning(
                '400 Bad Request — WebSocket handshake declares content; '
                'refusing to switch protocols. peer=%r', self._peername)
            await send(b'', HTTPStatus.BAD_REQUEST,
                       [(b'content-type', b'text/plain')])
            return False
        key = headers.get(b'sec-websocket-key', b'')
        # RFC 6455 §4.2.1 — the client MUST send a Sec-WebSocket-Key whose
        # base64-decoded value is 16 bytes.  An absent or malformed key is a
        # bad handshake; answer 400 rather than completing an accept hash over
        # the GUID alone (which some clients would then wrongly accept).
        try:
            valid_key = bool(key) and len(b64decode(key)) == 16
        except (ValueError, BinasciiError):
            valid_key = False
        if not valid_key:
            await send(b'', HTTPStatus.BAD_REQUEST,
                       [(b'content-type', b'text/plain')])
            return False
        accept_key = b64encode(sha1(key + _WS_GUID).digest())
        version = headers.get(b'sec-websocket-version', b'')
        if version != b'13':
            await send(b'', HTTPStatus.BAD_REQUEST,
                       [(b'sec-websocket-version', b'13')])
            return False

        client_protos = conn.subprotocols

        # Used when the handler calls websocket.accept without a subprotocol.
        available_raw = getattr(self._app, 'available_ws_protocols', [])
        available = {(p.decode('utf-8', errors='replace') if isinstance(p, bytes) else p)
                     for p in available_raw}
        auto_subprotocol = next((p for p in client_protos if p in available), None)

        # RFC 7692 permessage-deflate negotiation.  Cached on the Connection so
        # WebSocketActor can pick it up after the handshake commits, and
        # echoed back as ``Sec-WebSocket-Extensions`` in the 101 response.
        from .permessage_deflate import negotiate_offer as _negotiate_deflate  # noqa: PLC0415
        deflate_params, deflate_response = _negotiate_deflate(headers)

        async def _send_101(subprotocol=None, app_headers=None):
            hs_headers = Headers([
                (b'upgrade', b'websocket'),
                (b'connection', b'upgrade'),
                (b'sec-websocket-accept', accept_key),
            ])
            if subprotocol:
                sp = subprotocol.encode() if isinstance(subprotocol, str) else subprotocol
                hs_headers.append(b'sec-websocket-protocol', sp)
            if deflate_response is not None:
                hs_headers.append(b'sec-websocket-extensions', deflate_response)
            if app_headers:
                hs_headers.append(app_headers)
            await send(b'', HTTPStatus.SWITCHING_PROTOCOLS, hs_headers)

        conn._ws = {
            'send_101': _send_101,
            'auto_subprotocol': auto_subprotocol,
            'deflate': deflate_params,
        }
        return True

    async def _dispatch_request(
        self,
        conn: Connection,
        send,
        cfg,
        dl,
        inner_receive: 'HTTP1Recipient | None',
        loop_start_perf: float,
        loop_start_cpu: float,
    ) -> tuple[bool, 'HTTP1Recipient']:
        """Prepare and run a request; return (keep_alive, inner_receive).

        Rewrite HEAD to GET before RequestActor snapshots the compatibility scope.
        Keep the original method for access logs and suppress the response body.
        """
        # Open records only for access logs, phase trace or lifecycle consumers.
        if _request_record_needed(self._aggregator):
            log_record = _AccessLogRecord.from_conn(conn)
            if _PHASE_TRACE:
                log_record.phases['loop_start'] = (
                    loop_start_perf, loop_start_cpu)
            log_record.mark('parsed')
            conn.state['access_log'] = log_record
        else:
            log_record = None

        # Reset all per-request sender state before dispatch or interim responses.
        send.reset_per_request_state()

        # Ignore Expect on HTTP/1.0. Reset before sending 100; attach capture after it
        # so the final response record excludes the interim status.
        if conn.http_version != '1.0' and self._expects_continue:
            await send(b'', HTTPStatus.CONTINUE)

        # Dispatch HEAD to GET but suppress its body. Preserve the original HEAD
        # in access logs; changing Connection must not materialize an ASGI scope.
        send._log_record = log_record
        send._head_mode = method_is(conn.method, 'HEAD')
        if send._head_mode:
            conn.method = 'GET'

        # Reusable: reader, body timeout and deadline are all connection
        # properties.  ``bind`` re-derives the framing from the new head.
        first_request_on_connection = inner_receive is None
        if first_request_on_connection:
            inner_receive = RecipientFactory.http1(
                self._reader, conn, body_timeout=cfg.body_timeout, deadline=dl,
                framing=self._request_framing)
        else:
            inner_receive.bind(conn, self._request_framing)

        if conn._asterisk_form:
            # RFC 9112 §3.2.4 — ``OPTIONS *`` targets the origin, not a
            # resource, so it never routes.
            await send(b'', HTTPStatus.NO_CONTENT,
                       [(b'allow', _SERVER_WIDE_ALLOW)])
            return True, inner_receive

        request_actor = self._request_actor
        if request_actor is None:
            request_actor = self._request_actor = RequestActor(
                conn, inner_receive, send,
                self._app, self._aggregator, cfg.force_asgi_scope,
            )
        else:
            request_actor.bind(conn, inner_receive, send)
        try:
            await request_actor.run()
        except asyncio.CancelledError:
            # Let BB_REQUEST_TIMEOUT's wait_for see the cancellation;
            # swallowing it here would convert a timeout into a normal close
            # without the 408 synthesis.
            raise
        except Exception:
            return False, inner_receive
        finally:
            if log_record is not None:
                log_record.mark('dispatch_done')
                _emit_access_log(log_record)
        if send._response_started and not send._completed:
            # A response with an open body or trailer section has not reached
            # its declared wire boundary, so the next request cannot reuse it.
            return False, inner_receive
        return True, inner_receive

    async def _read_headers(self, max_total: int) -> None:
        """Read the next message head into ``self._request``.

        ``self._request`` is set on every exit path, including the failing
        ones: ``run()`` reads it to tell an idle close from a truncated
        request, and to report how many bytes an over-budget peer sent.
        """
        try:
            head = await self._reader.read_head(max_total)
        except ReadLimitExceeded as exc:
            # "No CRLF at all" would not do as the test: the whole request
            # usually arrives in one burst, terminator included, so the
            # question is whether the *first* CRLF is inside the budget.
            self._request = exc.seen
            _reject_oversized_head(exc.seen, max_total)
            raise   # unreachable: _reject_oversized_head always raises
        except IncompleteReadError as exc:
            self._request = exc.partial
            raise
        if not head:
            # Clean EOF with nothing sent — an idle peer closing, not a
            # truncated request.
            self._request = b''
            raise IncompleteReadError(b'')
        self._request = head

    def _should_keep_alive(self, conn) -> bool:
        """Return True if the connection should persist after this request."""
        http_version = conn.http_version
        fields = conn.headers._index.get(b'connection')
        connection = fields[0][1].lower() if fields else b''
        if http_version == '1.1':
            return connection != b'close'
        return connection == b'keep-alive'

    async def _handle(self, msg: Message) -> None:  # never reached
        raise NotImplementedError
