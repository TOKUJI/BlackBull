"""HTTP/1.1 Actor classes for the BlackBull actor model.

HTTP1Actor drives the keep-alive loop for one TCP connection.
RequestActor owns the lifetime of a single HTTP request.
"""
import asyncio
import logging
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
from ..protocol.framing import _CRLF, _HEAD_END, method_is
from ..protocol.http1 import (FieldsTooLarge, LineCache, MessageError,
                              ReadLimitExceeded, Unimplemented, UnsupportedVersion,
                              parse_head, persistent, request_framing)
from .deadline import Guard
from .request_target import split_path_query
from .recipient import (CONNECTION_MUST_CLOSE, CONNECTION_NEEDS_DRAIN,
                        AbstractReader, HTTP1Recipient, IncompleteReadError,
                        RecipientFactory, _WS_READ_INLINE)
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

from ..protocol.field_grammar import (
    METHOD_TEXT, URI_SCHEME_RE, VERSION_TEXT, FieldError, host_field_value,
    list_members)


def _reject_oversized_head(head: bytes, max_total: int) -> None:
    """Raise the right rejection for a head that overran the total budget.

    Always raises: **400** when the first CRLF lands beyond the budget, because
    the start-line never ended (RFC 9112 §3) and a 100 KiB method token is not
    "too many header fields"; **431** when the lines are well-formed and merely
    numerous (RFC 6585 §5).
    """
    first_eol = head.find(_CRLF)
    if first_eol < 0 or first_eol >= max_total:
        raise MessageError(
            f'request line exceeds BB_HEADER_MAX_TOTAL={max_total} '
            f'without a line terminator')
    raise FieldsTooLarge(
        f'header block {len(head)} bytes > BB_HEADER_MAX_TOTAL={max_total}',
        len(head), max_total)


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
    _line_cache: 'LineCache | None' = None
    #: Body length the current request declares; 0 when it declares none.
    _declared_body_len: int = 0
    #: ``(content_length, chunked)`` of the request ``_parse`` validated last.
    _request_framing: tuple[int | None, bool] = (None, False)
    _expects_continue: bool = False
    _persistent: bool = False

    def __init__(
        self,
        reader: AbstractReader,
        writer: AbstractWriter,
        app: Callable[..., Awaitable[None]],
        aggregator: 'EventAggregator | None',
        *,
        peername: tuple[str, int | None] | None = None,
        sockname: tuple[str, int | None] | None = None,
        ssl: bool = False,
        ws_queue_depth: int = _WS_READ_INLINE,
        deadline: Guard | None = None,
        connection_id: str = '',
    ) -> None:
        super().__init__()
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
            self._deadline = Guard()
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
                        with dl.within(idle_window):
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
                except FieldsTooLarge as exc:
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
                except MessageError as exc:
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
                except FieldsTooLarge as exc:
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
                except Unimplemented as exc:
                    # RFC 9112 §6.1 — nginx parity: 501, then close.
                    logger.warning('501 Not Implemented: %s', exc)
                    await self._send_error_and_close(
                        send, b'501 Not Implemented', HTTPStatus.NOT_IMPLEMENTED)
                    return
                except UnsupportedVersion as exc:
                    logger.warning('505 HTTP Version Not Supported: %s', exc)
                    await self._send_error_and_close(
                        send, b'505 HTTP Version Not Supported',
                        HTTPStatus.HTTP_VERSION_NOT_SUPPORTED)
                    return
                except MessageError as exc:
                    # RFC 9112 §3 / §5.  A malformed request is a smuggling
                    # vector candidate, so the connection always terminates
                    # rather than hunting for the next message boundary.
                    logger.warning('400 Bad Request: %s', exc)
                    await self._send_error_and_close(
                        send, b'400 Bad Request', HTTPStatus.BAD_REQUEST)
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
                if not self._persistent:
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
        Raises the codec's [`MessageError`][] family; a line over
        ``BB_HEADER_MAX_LINE`` is [`FieldsTooLarge`][].
        """
        cache = self._line_cache
        if cache is None:
            cache = self._line_cache = LineCache()
        (method, path, version), raw, index = parse_head(
            data, get_settings().header_max_line, cache, True)

        # Request-target form dispatch (RFC 9112 §3.2).  Four forms:
        # origin (``/path``), absolute (``http://host/path``), authority
        # (CONNECT only), and asterisk (``*``, server-wide OPTIONS).
        authority_override: bytes | None = None
        asterisk_form = False
        if method_is(method, 'CONNECT'):
            # authority-form target (§3.2.3) — tunnel establishment, which
            # BlackBull does not implement.  Answer 501, not a spurious 404.
            raise Unimplemented(
                f'CONNECT (tunneling) is not implemented: {path!r}')

        if path == b'*':
            # asterisk-form (§3.2.4) — a server-wide request, valid only for
            # OPTIONS.
            if method != b'OPTIONS':
                raise MessageError(
                    f'asterisk-form request-target is valid only for OPTIONS, '
                    f'not {method!r}')
            asterisk_form = True
        else:
            # Request-target octets — reject CTLs, DEL, and non-ASCII (§2.1 /
            # RFC 3986; MAL-NON-ASCII-URL).  Before the absolute-form rewrite
            # below, whose scheme and authority would otherwise reach the host
            # header with no per-value check.
            if not path or path.translate(None, _TARGET_ALLOWED_OCTETS):
                raise MessageError(f'invalid request-target {path!r}')
            if (_ss := path.find(b'://')) != -1 and b'/' not in path[:_ss]:
                # absolute-form (§3.2.2): ``scheme "://" authority
                # path-abempty``.  Rewrite it to origin-form and let the
                # authority override Host (§3.2.2 — the origin server MUST
                # ignore the Host header here).
                # RFC 3986 §3.1 — the target's scheme grammar.  conn.scheme
                # stays the connection's: the target must not claim https.
                if not URI_SCHEME_RE.fullmatch(path[:_ss]):
                    raise MessageError(
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
                    raise MessageError(
                        f'absolute-form request-target has empty authority: '
                        f'{path!r}')

        if asterisk_form:
            _decoded_path, _raw_path_b, _query_string = '*', b'*', b''
        else:
            _decoded_path, _raw_path_b, _query_string = split_path_query(path)

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

        # RFC 9112 §6 — framing rejected before any body byte is read.  ``run``
        # weighs the returned length against ``BB_MAX_BODY_SIZE``.
        self._request_framing = request_framing(headers)
        self._persistent = persistent(version, headers)
        self._declared_body_len = self._request_framing[0] or 0
        expect = index.get(b'expect')
        self._expects_continue = expect is not None and b'100-continue' in list_members(
            b','.join([value for _, value in expect]))
        try:
            host_value = host_field_value(index.get(b'host'))
        except FieldError as exc:
            raise MessageError(str(exc)) from None
        # RFC 9112 §3.2 / §7.2 — every HTTP/1.1 (and later 1.x) request MUST
        # carry a Host header (RFC9112-7.1-MISSING-HOST); only HTTP/1.0, which
        # predates Host, may omit it (COMP-HTTP10-NO-HOST).
        if version != b'HTTP/1.0' and host_value is None:
            raise MessageError(
                f'missing Host header on {version.decode("ascii")} request '
                f'(RFC 9112 §3.2)')

        conn = Connection(
            type='http',
            http_version=VERSION_TEXT.get(version) or version[5:].decode('utf-8'),
            method=METHOD_TEXT.get(method) or method.decode('utf-8'),
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
            if b'websocket' in list_members(b','.join([value for _, value in upgrade])):
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
        if any(self._request_framing):
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

    async def _handle(self, msg: Message) -> None:  # never reached
        raise NotImplementedError
