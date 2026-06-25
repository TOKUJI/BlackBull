"""HTTP/1.1 Actor classes for the BlackBull Actor model (Phase 6 Step 3).

HTTP1Actor drives the keep-alive loop for one TCP connection.
RequestActor owns the lifetime of a single HTTP request.
"""
import logging
import re
from base64 import b64encode
from collections.abc import Awaitable, Callable
from hashlib import sha1
from http import HTTPStatus
from typing import Any

from ..actor import Actor, Message
from ..event import Event
from ..event_aggregator import EventAggregator
from ..asgi import ASGIEvent
from ..headers import Headers
from .deadline import ConnectionDeadline
from .recipient import AbstractReader, IncompleteReadError, RecipientFactory, _WS_EVENT_QUEUE_DEPTH
from .sender import AbstractWriter, SenderFactory
from .access_log import AccessLogRecord as _AccessLogRecord, _make_disconnect_detecting_receive, emit_access_log as _emit_access_log
from .cap_log import log_cap_hit

logger = logging.getLogger(__name__)

_REQ_END = b'\r\n\r\n'
_WS_GUID = b'258EAFA5-E914-47DA-95CA-C5AB0DC85B11'  # RFC 6455 §1.3
_HTTP_PORT  = 80
_HTTPS_PORT = 443

# Extensions advertised in scope['extensions'] for cleartext HTTP/1.1.
# ``http.response.pathsend`` lets middleware (notably the static-file
# serve path) hand a file path to the sender so the body bytes go
# through ``loop.sendfile`` — zero-copy on Linux, no per-chunk
# event-loop dispatch overhead.  TLS connections do NOT advertise it
# because ``loop.sendfile`` raises NotImplementedError on SSL
# transports (the kernel can't see the plaintext to copy).
_H1_PATHSEND_EXTENSIONS = {'http.response.pathsend': {}}

# RFC 9110 §5.6.2 — token = 1*tchar
# RFC 9110 §5.5  — field-value disallows CTLs (0x00-0x1F, 0x7F) except HTAB
# RFC 9112 §4   — method = token, HTTP-version = "HTTP/" DIGIT "." DIGIT
_TOKEN_CHARS = (
    b"!#$%&'*+-.^_`|~"
    + bytes(range(0x30, 0x3A))   # 0-9
    + bytes(range(0x41, 0x5B))   # A-Z
    + bytes(range(0x61, 0x7B))   # a-z
)
_TOKEN_SET = frozenset(_TOKEN_CHARS)
_HTTP_VERSION_RE = re.compile(rb'^HTTP/\d\.\d$')

# Sprint 25 Phase B — compiled-regex validators replace per-byte
# `any(c not in _TOKEN_SET for c in …)` and `any((b < 0x20 or
# b == 0x7F) and b != 0x09 for b in …)` scans in `_parse`.  The
# character classes below are the exact negation of the RFC 9110
# §5.6.2 tchar set and the RFC 9110 §5.5 CTL-except-HTAB allow-list
# respectively; `re.search` returns a Match on the first invalid
# byte (3–4× faster than the equivalent Python loop per pyperf).
# `_TOKEN_SET` is retained for any external callers that may rely
# on it.
_FIELD_NAME_INVALID_RE = re.compile(rb"[^!#$%&'*+\-.^_`|~0-9A-Za-z]")
_FIELD_VALUE_INVALID_RE = re.compile(rb"[\x00-\x08\x0a-\x1f\x7f]")


class BadRequestError(Exception):
    """Raised by :meth:`HTTP1Actor._parse` on an RFC 9112 framing violation.

    The actor's keep-alive loop catches this and sends a 400 Bad Request
    before closing the connection — never tries to dispatch the malformed
    request to the app.
    """


class HeaderTooLargeError(Exception):
    """Raised when a request header line or the whole header block exceeds
    the configured limit (``BB_HEADER_MAX_LINE`` / ``BB_HEADER_MAX_TOTAL``).

    The actor answers with 431 Request Header Fields Too Large (RFC 6585
    §5) and closes the connection.  Distinct from :class:`BadRequestError`
    because the response status differs.
    """


class NotImplementedFramingError(Exception):
    """RFC 9112 §6.1 — the request used a Transfer-Encoding the server
    does not implement.  Answered with 501 Not Implemented (a separate
    response code from :class:`BadRequestError`'s 400)."""


def _validate_message_framing(headers: 'Headers') -> None:
    """RFC 9112 §6 — reject framing-header combinations that are unsafe.

    These are the rules every smuggling-class incident I'm aware of has
    exploited.  Specifically:

    * §6.2 — ``Content-Length`` value MUST be ``1*DIGIT`` (no signs, no
      whitespace, non-empty).
    * §6.2 — multiple ``Content-Length`` headers MUST all have the same
      single integer value.  Different values are a CL.CL vector.
    * §6.1 — if both ``Content-Length`` and ``Transfer-Encoding`` are
      present, the message is anomalous.  We reject (the spec also
      allows "ignore CL, use TE"; rejecting is the safer policy).
    * §6.1 — unknown ``Transfer-Encoding`` codings → 501 Not Implemented.
      We accept exactly ``chunked``; anything else (``gzip``, the
      ``identity, chunked`` multi-coding form, etc.) raises
      :class:`NotImplementedFramingError`.  Without this check the
      recipient layer raised ``NotImplementedError`` later and the
      connection dropped silently (Sprint 17 Finding C).
    """
    cls = headers.getlist(b'content-length')
    tes = headers.getlist(b'transfer-encoding')

    if cls and tes:
        raise BadRequestError(
            'Content-Length and Transfer-Encoding both present '
            '(smuggling vector)')

    if cls:
        # Collapse comma-combined and multi-header into a single set of values.
        values: set[bytes] = set()
        for _, value in cls:
            for v in value.split(b','):
                v = v.strip()
                if not v or not v.isdigit():
                    raise BadRequestError(f'invalid Content-Length value {v!r}')
                # Strip leading zeros so "00005" and "5" compare equal.
                values.add(v.lstrip(b'0') or b'0')
        if len(values) > 1:
            raise BadRequestError(
                f'conflicting Content-Length values: {sorted(values)!r}')

    # Sprint 18 Phase 2 — TE coding validation.  Match nginx's behaviour:
    # only the bare ``chunked`` token is accepted; everything else (gzip,
    # deflate, identity-as-comma-list, etc.) is 501.  Both nginx and
    # BlackBull's corpus showed nginx returning 501 on ``identity,
    # chunked`` and ``gzip``.
    for _, raw_value in tes:
        te = raw_value.strip().lower()
        if te != b'chunked':
            raise NotImplementedFramingError(
                f'Transfer-Encoding {raw_value!r} is not implemented')


# RFC 3986 §3.2 — authority = [userinfo "@"] host [":" port].  None of
# these delimiter octets belong in a Host header value; their presence
# (or an empty value) is a smuggling / SSRF vector that nginx rejects
# with 400 and BlackBull, pre-Sprint-18, accepted silently.
_HOST_FORBIDDEN_BYTES = frozenset(b'/?# \t')


def _validate_host(headers: 'Headers') -> None:
    """RFC 9112 §3.2 / §7.2 — Host MUST be present and contain a valid
    URI-authority component.  Sprint 17 Finding B captured several
    inputs (``host: 0/0``, empty host) where BlackBull answered 200 and
    nginx answered 400.  This check brings BlackBull into RFC alignment.

    Rules enforced:
      * at most one Host header (§7.2; multiple is a smuggling vector);
      * non-empty value after OWS-stripping;
      * no ``/`` / ``?`` / ``#`` / whitespace in the authority
        (RFC 3986 §3.2 delimiters).
    """
    hosts = headers.getlist(b'host')
    if len(hosts) > 1:
        raise BadRequestError(
            f'multiple Host headers ({len(hosts)} — smuggling vector)')
    if not hosts:
        # HTTP/1.1 §3.2 requires Host but HTTP/1.0 doesn't; we don't
        # check version here because absent-Host on 1.1 is already
        # handled elsewhere if at all.  Leaving the strict-absent
        # check to a separate Sprint 18 follow-up keeps the patch
        # focused on the captured-corpus divergences.
        return
    value = hosts[0][1].strip(b' \t')
    if not value:
        raise BadRequestError('empty Host header value')
    if any(b in _HOST_FORBIDDEN_BYTES for b in value):
        raise BadRequestError(
            f'invalid Host authority {value!r}: contains '
            f'delimiter / whitespace forbidden by RFC 3986 §3.2')


# ---------------------------------------------------------------------------
# RequestActor — single HTTP request lifetime
# ---------------------------------------------------------------------------

class RequestActor(Actor):
    """Owns one HTTP/1.1 request lifetime.

    Spawned by HTTP1Actor, awaited to completion.  Calls the ASGI app and
    emits before_handler / after_handler / request_completed / error via
    EventAggregator.

    When no Level B event listeners are registered, the entire aggregator
    indirection is skipped and the app is called directly — matching the
    pre-Sprint-53 hot path and avoiding ~4 async function-call frames
    per request.
    """

    def __init__(
        self,
        scope: dict[str, Any],
        receive: Callable[..., Awaitable[Any]],
        send: Callable[..., Awaitable[Any]],
        app: Callable[..., Awaitable[None]],
        aggregator: EventAggregator,
    ) -> None:
        super().__init__()
        self._scope = scope
        self._receive = receive
        self._send = send
        self._app = app
        self._aggregator = aggregator

    async def run(self) -> None:  # override: single-shot, no inbox loop
        # Fast path: when no Level B event handlers are registered at
        # all, skip the entire EventAggregator indirection and call the
        # app directly.  This reclaims the ~4 async function-call frames
        # per request that Sprint 53 added for the MQTT broker pattern.
        if not self._aggregator.has_any_request_listeners():
            await self._call_app(self._scope, self._receive, self._send)
            return

        await self._aggregator.on_request_received(self._scope)
        exc: BaseException | None = None
        try:
            await self._aggregator.on_before_handler(
                self._scope, self._receive, self._send,
                call_next=self._call_app,
            )
        except BaseException as e:
            exc = e
            await self._aggregator.on_error(self._scope, e)
            raise
        finally:
            await self._aggregator.on_after_handler(self._scope, exception=exc)
            if exc is None:
                await self._aggregator.on_request_completed(self._scope)

    async def _call_app(
        self,
        scope: dict[str, Any],
        receive: Callable[..., Awaitable[Any]],
        send: Callable[..., Awaitable[Any]],
    ) -> None:
        await self._app(scope, receive, send)

    async def _handle(self, msg: Message) -> None:  # never reached
        raise NotImplementedError


# ---------------------------------------------------------------------------
# HTTP1Actor — keep-alive connection loop
# ---------------------------------------------------------------------------

class HTTP1Actor(Actor):
    """Drives the HTTP/1.1 keep-alive loop for one connection.

    Supervisor strategy: isolate — an unhandled exception from a RequestActor
    closes the connection without crashing sibling connections.

    If *aggregator* is ``None`` the actor falls back to the legacy direct-
    dispatcher path (fires events via ``app._dispatcher`` directly), so that
    BlackBull apps without a full EventAggregator still receive lifecycle events.
    """

    # Class-level default so tests that instantiate via ``object.__new__``
    # without calling ``__init__`` (test_parser.py uses this pattern to
    # exercise ``_parse`` in isolation) see a cleartext scope by default.
    _ssl: bool = False

    def __init__(
        self,
        reader: AbstractReader,
        writer: AbstractWriter,
        app: Callable[..., Awaitable[None]],
        aggregator: 'EventAggregator | None',
        *,
        request: bytes = b'',
        peername: tuple[str, int] | None = None,
        sockname: tuple[str, int] | None = None,
        ssl: bool = False,
        ws_queue_depth: int = _WS_EVENT_QUEUE_DEPTH,
        deadline: ConnectionDeadline | None = None,
    ) -> None:
        super().__init__()
        self._reader = reader
        self._writer = writer
        self._app = app
        self._aggregator = aggregator
        self._request = request
        self._peername = peername
        self._sockname = sockname
        self._ssl = ssl
        self._ws_queue_depth = ws_queue_depth
        # When the actor is constructed without a deadline (test
        # fixtures that drive the actor directly), one is lazily
        # created on entry to ``run()`` so the production hot path and
        # the test path share the same code.
        self._deadline = deadline

    async def run(self) -> None:
        """Keep-alive loop — process requests until connection closes."""
        import asyncio  # noqa: PLC0415
        import time as _time  # noqa: PLC0415
        from ..env import get_settings as _get_settings  # noqa: PLC0415
        from .access_log import PHASE_TRACE as _PHASE_TRACE  # noqa: PLC0415
        cfg = _get_settings()
        # Sprint 23: one rescheduled TimerHandle per connection drives
        # all phase deadlines (headers / body / keep-alive).  Created
        # lazily so tests that instantiate HTTP1Actor without a wrapping
        # ConnectionActor still work — same task either way.
        if self._deadline is None:
            self._deadline = ConnectionDeadline()
        dl = self._deadline
        send = SenderFactory.http1(self._writer)
        # Sprint 33 investigation: capture loop_start at the very top
        # of each iteration so we can quantify the between-request gap
        # (dispatch_done(N) → loop_start(N+1)) on a keep-alive
        # connection.  Plain locals — assigned to log_record.phases
        # below once the record exists.
        _loop_start_perf: float = 0.0
        _loop_start_cpu: float = 0.0
        try:
            while True:
                if _PHASE_TRACE:
                    _loop_start_perf = _time.perf_counter()
                    _loop_start_cpu = _time.process_time()
                # Slowloris defence (RFC 9110 §15.5.9 — 408 Request Timeout).
                # The peer has a bounded window to send the complete header
                # block; if it elapses we close the connection with 408 so a
                # well-behaved monitoring client can tell us apart from a
                # peer-side disconnect.  ``header_timeout=0`` disables the
                # deadline (legacy behaviour for trusted local use).
                try:
                    if cfg.header_timeout > 0:
                        with dl.guard(cfg.header_timeout):
                            await self._read_headers(cfg.header_max_total)
                    else:
                        await self._read_headers(cfg.header_max_total)
                except IncompleteReadError:
                    # Sprint 17 Phase 3 — distinguish idle EOF from
                    # mid-headers EOF.  ``self._request`` is empty in
                    # the idle case (just-after keep-alive reset or
                    # fresh connection with no preamble); peer closed
                    # without sending anything ⇒ silent close.
                    # Non-empty buffer ⇒ peer sent partial bytes then
                    # disconnected ⇒ 400 Bad Request before close, so
                    # differential tests can categorise this as a
                    # protocol violation rather than a transport reset.
                    if self._request:
                        logger.info(
                            '400 Bad Request — peer EOF mid-headers '
                            'after %d bytes; peer=%r',
                            len(self._request), self._peername,
                        )
                        await send(
                            b'400 Bad Request',
                            HTTPStatus.BAD_REQUEST,
                            [(b'connection', b'close'),
                             (b'content-type', b'text/plain')],
                        )
                    return
                except HeaderTooLargeError as exc:
                    # RFC 6585 §5 — 431 Request Header Fields Too Large.
                    # The buffer is over the configured budget; close so an
                    # attacker can't keep feeding us bytes after the reply.
                    logger.warning('431 Request Header Fields Too Large: %s', exc)
                    log_cap_hit('header_max_total',
                                requested=len(self._request),
                                limit=cfg.header_max_total,
                                peer=self._peername, protocol='http1')
                    await send(
                        b'431 Request Header Fields Too Large',
                        HTTPStatus.REQUEST_HEADER_FIELDS_TOO_LARGE,
                        [(b'connection', b'close'),
                         (b'content-type', b'text/plain')],
                    )
                    return
                except (asyncio.TimeoutError, TimeoutError):
                    logger.warning(
                        '408 Request Timeout (slowloris defence) — peer=%r '
                        'sent %d bytes in %.1fs without completing headers',
                        self._peername, len(self._request), cfg.header_timeout)
                    log_cap_hit('header_timeout',
                                requested=cfg.header_timeout,
                                limit=cfg.header_timeout,
                                peer=self._peername, protocol='http1')
                    await send(
                        b'408 Request Timeout',
                        HTTPStatus.REQUEST_TIMEOUT,
                        [(b'connection', b'close'),
                         (b'content-type', b'text/plain')],
                    )
                    return

                try:
                    scope = self._parse(self._request)
                except HeaderTooLargeError as exc:
                    # Per-line limit hit during parse.  Same response as the
                    # total-block check in _read_headers.
                    logger.warning('431 Request Header Fields Too Large: %s', exc)
                    log_cap_hit('header_max_line',
                                requested=len(self._request),
                                limit=cfg.header_max_line,
                                peer=self._peername, protocol='http1')
                    await send(
                        b'431 Request Header Fields Too Large',
                        HTTPStatus.REQUEST_HEADER_FIELDS_TOO_LARGE,
                        [(b'connection', b'close'),
                         (b'content-type', b'text/plain')],
                    )
                    return
                except BadRequestError as exc:
                    # RFC 9112 §3 / §5 violation — answer with 400 and close.
                    # A malformed request is a smuggling vector candidate, so
                    # we always terminate the connection rather than try to
                    # find the next message boundary.
                    logger.warning('400 Bad Request: %s', exc)
                    await send(
                        b'400 Bad Request',
                        HTTPStatus.BAD_REQUEST,
                        [(b'connection', b'close'),
                         (b'content-type', b'text/plain')],
                    )
                    return
                except NotImplementedFramingError as exc:
                    # Sprint 18 Phase 2 — RFC 9112 §6.1: server received a
                    # Transfer-Encoding it does not implement.  Pre-Sprint-18
                    # this raised inside HTTP1Recipient and the connection
                    # dropped silently (Finding C in user-corpus).  Match
                    # nginx and answer with 501 then close.
                    logger.warning('501 Not Implemented: %s', exc)
                    await send(
                        b'501 Not Implemented',
                        HTTPStatus.NOT_IMPLEMENTED,
                        [(b'connection', b'close'),
                         (b'content-type', b'text/plain')],
                    )
                    return
                self._fill_connection_info(scope)

                if scope.get('type') == 'websocket':
                    await self._handle_upgrade(scope)
                    return

                if scope['headers'].get(b'expect').lower() == b'100-continue':
                    await send(b'', HTTPStatus.CONTINUE)

                log_record = _AccessLogRecord.from_scope(scope)
                if _PHASE_TRACE:
                    log_record.phases['loop_start'] = (
                        _loop_start_perf, _loop_start_cpu)
                log_record.mark('parsed')
                scope['state']['access_log'] = log_record
                # Sprint 38 Task B — reset per-request sender state.  The
                # HTTP1Sender instance is shared across keep-alive
                # requests on this connection; without this reset
                # ``_started`` stays True after the first response and
                # the timeout branch's ``if not send._started`` check
                # would skip the synthetic 408 on a second-or-later
                # request.  ``_chunked`` / ``_buffered_status`` similarly
                # outlive their request.  Encapsulated in the sender
                # so adding a new per-request slot can't be silently
                # missed at this call site.
                send.reset_per_request_state()
                # Inline access-log capture into the sender itself —
                # avoids the per-event coroutine dispatch through a
                # wrapper (which was 622 samples / 7% of CPU in the
                # py-spy profile).  The sender's existing match arms
                # already pattern-match on the event types we care
                # about; updating ``log_record`` there is free.
                send._log_record = log_record
                capturing_send = send
                # RFC 9110 §9.3.2 — a HEAD response must be identical to
                # the GET response except for the absence of the body.
                # We synthesise that by dispatching to the GET handler
                # and stripping body bytes from outgoing events.
                # ``method`` on the scope is rewritten so the router
                # (and any handler that inspects scope['method']) sees
                # ``GET``; the access log records the original ``HEAD``
                # from the request line.
                send._head_mode = (scope['method'] == 'HEAD')
                if send._head_mode:
                    scope['method'] = 'GET'
                inner_receive = RecipientFactory.http1(
                    self._reader, scope,
                    body_timeout=cfg.body_timeout,
                    deadline=dl,
                )

                # Sprint 38 Task B — BB_REQUEST_TIMEOUT parity with the
                # HTTP/2 path.  ``HTTP2Actor._spawn_stream_task`` wraps each
                # stream coroutine with ``asyncio.wait_for``; the HTTP/1.1
                # path mirrors that here.  On expiry: synthesise 408 if
                # headers haven't shipped yet, then close the connection
                # (no keep-alive across a timed-out request).
                try:
                    if cfg.request_timeout > 0:
                        ok = await asyncio.wait_for(
                            self._dispatch_request(
                                scope, inner_receive, capturing_send, log_record),
                            timeout=cfg.request_timeout,
                        )
                    else:
                        ok = await self._dispatch_request(
                            scope, inner_receive, capturing_send, log_record)
                except (asyncio.TimeoutError, TimeoutError):
                    logger.warning(
                        '408 Request Timeout — handler on %s %s exceeded '
                        'BB_REQUEST_TIMEOUT=%.1fs; closing connection',
                        scope.get('method', '?'), scope.get('path', '?'),
                        cfg.request_timeout,
                    )
                    log_cap_hit('request_timeout',
                                requested=cfg.request_timeout,
                                limit=cfg.request_timeout,
                                peer=self._peername,
                                scope_path=scope.get('path'),
                                protocol='http1')
                    if not send._started:
                        await send(
                            b'408 Request Timeout',
                            HTTPStatus.REQUEST_TIMEOUT,
                            [(b'connection', b'close'),
                             (b'content-type', b'text/plain')],
                        )
                    break
                if not ok:
                    break  # unhandled error — close connection

                # RFC 9112 §9.1 — honour Connection: close.  HTTP/1.0
                # connections without ``Connection: keep-alive`` likewise
                # default to non-persistent.
                if not self._should_keep_alive(scope):
                    break

                self._request = b''
                # Idle keep-alive timeout (replaces per-accept SO_KEEPALIVE).
                # The first request used header_timeout above; *subsequent*
                # requests are bounded by keep_alive_timeout so a peer
                # that has vanished silently (process crash, NAT drop,
                # mobile network change) doesn't hold a ghost connection.
                try:
                    if cfg.keep_alive_timeout > 0:
                        with dl.guard(cfg.keep_alive_timeout):
                            next_chunk = await self._reader.readuntil(_REQ_END)
                    else:
                        next_chunk = await self._reader.readuntil(_REQ_END)
                except TimeoutError:
                    break  # idle too long — drop the connection
                except IncompleteReadError:
                    # Peer cleanly closed between requests — silent close
                    # is correct (no 400, no 408): the previous response
                    # already shipped, and the spec allows either side
                    # to close a persistent connection at any time.
                    break
                if next_chunk == _REQ_END:
                    break
                self._request = next_chunk

        except IncompleteReadError:
            # Safety net: any IncompleteReadError that escapes the
            # explicit catches above (e.g. body-read EOF that wasn't
            # absorbed by HTTP1Recipient).  Surface http.disconnect
            # to the ASGI sender and let the connection close.
            await send({'type': ASGIEvent.HTTP_DISCONNECT})

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _parse(self, data: bytes) -> dict:
        """Parse raw HTTP/1.1 request bytes into an ASGI scope dict.

        Raises :class:`BadRequestError` on any RFC 9112 framing violation
        the caller should answer with 400.  Validation rules:

        * request-line: exactly ``method SP request-target SP HTTP-version``;
          method/version are validated against the token / HTTP-version
          grammar (§4, RFC 9110 §5.6.2).
        * field-line: ``name ':' OWS value OWS``; no whitespace between
          name and colon (§5.1); no obs-fold (§5.2); no CTLs in value
          except HTAB (RFC 9110 §5.5); name must be a valid token.

        Raises :class:`HeaderTooLargeError` when any request-line or
        header line exceeds ``BB_HEADER_MAX_LINE`` (default 8 KiB).  The
        whole-block limit (``BB_HEADER_MAX_TOTAL``) is enforced in
        ``run()`` because it sees the accumulating buffer; per-line is
        cheaper to check here, post-split.
        """
        from ..env import get_settings as _get_settings  # noqa: PLC0415
        max_line = _get_settings().header_max_line
        lines = data.split(b'\r\n')
        if max_line > 0:
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

        # Method (§4 / RFC 9110 §9.1) — case-sensitive token of 1+ tchar.
        if not method or _FIELD_NAME_INVALID_RE.search(method):
            raise BadRequestError(f'invalid method {method!r}')

        # HTTP-version (§2.5) — exactly ``HTTP/d.d``.
        if not _HTTP_VERSION_RE.match(version):
            raise BadRequestError(f'invalid HTTP-version {version!r}')

        # Request-target — for origin-form, leading slash; reject CTLs.
        if not path or any(b < 0x21 or b == 0x7F for b in path):
            raise BadRequestError(f'invalid request-target {path!r}')

        # Sprint 25 Phase A — replicate urllib.parse.urlparse() semantics
        # on the bytes request-target with three C-level partition calls:
        # strip #fragment, split ?query, separate ;params.  ~12× faster
        # than urlparse (pyperf microbench).  Output is byte-for-byte
        # equivalent for the .path + .query attributes we use.
        _no_frag, _, _ = path.partition(b'#')
        _path_and_params, _, _query_string = _no_frag.partition(b'?')
        _scope_path_b, _, _ = _path_and_params.partition(b';')

        raw: list[tuple[bytes, bytes]] = []
        for line in lines[idx + 1:]:
            if not line:
                # Empty line = end of headers; anything after is body (already
                # split off upstream because we read until CRLFCRLF).
                continue
            # RFC 9112 §5.2 — obs-fold (leading SP/HTAB on a header line)
            # MUST be rejected in requests.
            if line[:1] in (b' ', b'\t'):
                raise BadRequestError(
                    f'obsolete line folding rejected: {line!r}')
            colon = line.find(b':')
            if colon < 1:
                raise BadRequestError(f'malformed header line: {line!r}')
            key = line[:colon]
            value = line[colon + 1:]
            # §5.1 — no whitespace between field-name and ':'.
            if key[-1:] in (b' ', b'\t'):
                raise BadRequestError(
                    f'whitespace before colon (smuggling vector): {line!r}')
            # field-name must be a valid token (§5.1 / RFC 9110 §5.6.2).
            if _FIELD_NAME_INVALID_RE.search(key):
                raise BadRequestError(f'invalid header name {key!r}')
            # Strip the OWS surrounding the value (§5).
            value = value.strip(b' \t')
            # field-value MUST NOT contain CTLs except HTAB.
            if _FIELD_VALUE_INVALID_RE.search(value):
                raise BadRequestError(
                    f'CTL in header value (smuggling / log-injection): '
                    f'{key!r}: {value!r}')
            raw.append((key.lower(), value))
        headers = Headers(raw)

        # RFC 9112 §6 — message framing validation.  Done here so a bad
        # framing header is rejected before any body bytes are read.
        # Sprint 18 — also validates Host (§3.2 / §7.2) and the
        # transfer-coding registry (§6.1 → 501 on unknown coding).
        _validate_message_framing(headers)
        _validate_host(headers)

        scope = {
            'type': 'http',
            'asgi': {'version': '3.0', 'spec_version': '2.0'},
            'http_version': version[5:].decode('utf-8'),
            'method': method.decode('utf-8'),
            'scheme': 'http',
            'path': _scope_path_b.decode('utf-8'),
            'raw_path': path,
            'query_string': _query_string,
            'root_path': headers.get(b'X-Forwarded-Prefix', b'').decode('utf-8'),
            'headers': headers,
            'client': None,
            'server': None,
            'state': {},
            'extensions': _H1_PATHSEND_EXTENSIONS if not self._ssl else {},
        }

        if headers.getlist(b'host'):
            parts = headers.get(b'host').split(b':')
            host = parts[0]
            port = int(parts[1]) if len(parts) > 1 else (_HTTPS_PORT if self._ssl else _HTTP_PORT)
            scope['server'] = [host.decode('utf-8'), port]

        if headers.getlist(b'upgrade'):
            scope['type'] = headers.get(b'upgrade').decode('utf-8').lower()
            if scope['type'] == 'websocket':
                scope['scheme'] = 'ws'

        return scope

    def _fill_connection_info(self, scope: dict) -> None:
        if self._peername is not None:
            scope['client'] = list(self._peername)

        if scope.get('server') is None and self._sockname is not None:
            scope['server'] = list(self._sockname)

        if self._ssl:
            scope['scheme'] = 'wss' if scope.get('type') == 'websocket' else 'https'

    async def _handle_upgrade(self, scope: dict) -> None:
        """Handle WebSocket upgrade."""
        from uuid import uuid4  # noqa: PLC0415
        from .websocket_actor import WebSocketActor  # noqa: PLC0415
        aggregator = self._aggregator
        if aggregator is None:
            # No aggregator — use a silent dispatcher so WebSocketActor can fire
            # lifecycle events without any subscribers receiving them.
            from ..event import EventDispatcher  # noqa: PLC0415
            from ..event_aggregator import EventAggregator  # noqa: PLC0415
            aggregator = EventAggregator(EventDispatcher())

        log_record = _AccessLogRecord.from_scope(scope)
        log_record.status = 101  # HTTP 101 Switching Protocols

        if not await self._do_ws_handshake(scope):
            return  # version check failed; 400 already sent
        scope['_connection_id'] = str(uuid4())
        ws_actor = WebSocketActor(
            self._reader, self._writer, scope, self._app, aggregator,
            peername=self._peername, sockname=self._sockname, ssl=self._ssl,
            ws_queue_depth=self._ws_queue_depth,
        )
        try:
            await ws_actor.run()
        finally:
            log_record.close_code = ws_actor._disconnect_code
            _emit_access_log(log_record)

    async def _do_ws_handshake(self, scope: dict) -> bool:
        """Validate the WebSocket upgrade and store a deferred 101 callback.

        Returns True if the handshake is valid and ready to proceed, False if
        a 400 Bad Request was already sent (bad Sec-WebSocket-Version).

        The actual HTTP 101 response is deferred: it is sent by
        WebSocketActor._send when the ASGI app calls websocket.accept, so that
        the chosen subprotocol from that event can be included in the 101 headers
        (RFC 6455 §4.2.2).
        """
        send = SenderFactory.http1(self._writer)
        headers = scope.get('headers', Headers([]))
        key = headers.get(b'sec-websocket-key', b'')
        accept_key = b64encode(sha1(key + _WS_GUID).digest())
        version = headers.get(b'sec-websocket-version', b'')
        if version != b'13':
            await send(b'', HTTPStatus.BAD_REQUEST,
                       [(b'sec-websocket-version', b'13')])
            return False

        # Populate scope['subprotocols'] per ASGI WebSocket spec
        raw_sp = headers.get(b'sec-websocket-protocol', b'')
        client_protos = (
            [p.strip().decode('utf-8', errors='replace') for p in raw_sp.split(b',')]
            if raw_sp else []
        )
        scope['subprotocols'] = client_protos

        # Auto-negotiate from app.available_ws_protocols (backward-compat fallback).
        # This is used when the handler calls websocket.accept without a subprotocol.
        available_raw = getattr(self._app, 'available_ws_protocols', [])
        available = {(p.decode('utf-8', errors='replace') if isinstance(p, bytes) else p)
                     for p in available_raw}
        auto_subprotocol = next((p for p in client_protos if p in available), None)
        scope['_ws_auto_subprotocol'] = auto_subprotocol

        # RFC 7692 permessage-deflate negotiation.  Cached on the scope so
        # WebSocketActor can pick it up after the handshake commits, and
        # echoed back as ``Sec-WebSocket-Extensions`` in the 101 response.
        from ..env import get_settings as _get_settings  # noqa: PLC0415
        from .permessage_deflate import negotiate as _negotiate_deflate  # noqa: PLC0415
        deflate_params = None
        deflate_response = None
        if _get_settings().ws_permessage_deflate:
            offer = headers.get(b'sec-websocket-extensions', b'')
            deflate_params, deflate_response = _negotiate_deflate(offer or None)
        scope['_ws_deflate'] = deflate_params

        async def _send_101(subprotocol=None):
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
            await send(b'', HTTPStatus.SWITCHING_PROTOCOLS, hs_headers)

        scope['_ws_send_101'] = _send_101
        return True

    @staticmethod
    def _make_legacy_disconnect_receive(receive, scope: dict, dispatcher, log_record):
        """Legacy disconnect-detecting receive wrapper (mirrors server.py helper)."""
        async def detecting_receive():
            event = await receive()
            if isinstance(event, dict) and event.get('type') == ASGIEvent.HTTP_DISCONNECT:
                if not scope.get('_disconnected'):
                    scope['_disconnected'] = True
                    await dispatcher.emit(Event(
                        'request_disconnected',
                        detail={
                            'scope':        scope,
                            'client_ip':    log_record.client_ip,
                            'method':       log_record.method,
                            'path':         log_record.path,
                            'http_version': log_record.http_version,
                        },
                    ))
            return event
        return detecting_receive

    async def _dispatch_request(
        self,
        scope: dict,
        inner_receive,
        capturing_send,
        log_record,
    ) -> bool:
        """Dispatch one HTTP request via the aggregator or legacy path.

        Returns True on success, False if an unhandled error should close the connection.
        """
        import asyncio  # noqa: PLC0415
        if self._aggregator is not None:
            detecting_receive = _make_disconnect_detecting_receive(
                inner_receive, scope, self._aggregator)
            request_actor = RequestActor(
                scope, detecting_receive, capturing_send,
                self._app, self._aggregator,
            )
            try:
                await request_actor.run()
            except asyncio.CancelledError:
                # Sprint 38 Task B — let BB_REQUEST_TIMEOUT's wait_for see
                # the cancellation; swallowing it here would convert a
                # timeout into a normal close without the 408 synthesis.
                raise
            except BaseException:
                return False
            finally:
                log_record.mark('dispatch_done')
                _emit_access_log(log_record)
        else:
            _dispatcher = getattr(self._app, '_dispatcher', None)
            if _dispatcher is not None:
                detecting_receive = self._make_legacy_disconnect_receive(
                    inner_receive, scope, _dispatcher, log_record)
            else:
                detecting_receive = inner_receive
            try:
                if _dispatcher is not None:
                    await _dispatcher.emit(Event(
                        'request_received',
                        detail={
                            'scope':        scope,
                            'client_ip':    log_record.client_ip,
                            'method':       log_record.method,
                            'path':         log_record.path,
                            'http_version': log_record.http_version,
                            'headers':      scope.get('headers', []),
                        },
                    ))
                await self._app(scope, detecting_receive, capturing_send)
            finally:
                log_record.mark('dispatch_done')
                _emit_access_log(log_record)
                if (_dispatcher is not None
                        and scope.get('type') == 'http'
                        and not scope.get('_disconnected')):
                    await _dispatcher.emit(Event(
                        'request_completed',
                        detail={
                            'scope':          scope,
                            'client_ip':      log_record.client_ip,
                            'method':         log_record.method,
                            'path':           log_record.path,
                            'http_version':   log_record.http_version,
                            'status':         log_record.status,
                            'response_bytes': log_record.response_bytes,
                            'duration_ms':    log_record.duration_ms(),
                        },
                    ))
        return True

    async def _read_headers(self, max_total: int) -> None:
        """Drain bytes from the reader until ``\\r\\n\\r\\n`` is at the end of
        ``self._request``.  Enforces the configured total-block size limit;
        raises :class:`HeaderTooLargeError` when the buffer overshoots.

        Read **one CRLF-terminated line per iteration** rather than scanning
        for the contiguous ``\\r\\n\\r\\n`` delimiter.  Sprint 19 — the
        ``readuntil(b'\\r\\n\\r\\n')`` shape deadlocked when
        :class:`ConnectionActor` had already consumed the first line's
        ``\\r\\n`` via its protocol-detect ``readuntil(b'\\r\\n')`` and the
        remaining buffer contained only the terminating empty line's
        ``\\r\\n`` (two bytes, half of the contiguous delimiter the loop
        was searching for).  A minimally-valid HTTP/1.0 request
        (``GET / HTTP/1.0\\r\\n\\r\\n``, no headers) would hang here until
        the client closed its write side.  Reading line-by-line handles
        the case naturally: each iteration consumes one CRLF, and the
        empty header-block terminator (line == ``b'\\r\\n'``) makes
        ``self._request`` end with ``\\r\\n\\r\\n`` regardless of how the
        request was split across the two reader stages.

        asyncio's StreamReader has its own buffer limit (default 64 KiB,
        triggering ``LimitOverrunError``) which is converted into the same
        :class:`HeaderTooLargeError` here so callers can handle one
        exception class regardless of which side caught the overflow.
        """
        import asyncio  # noqa: PLC0415
        # Accumulate into a bytearray (amortised O(1) append) instead of the
        # O(n²) bytes ``+=`` growth, then publish back as bytes.  The loop
        # condition and size check are byte-for-byte equivalent to the prior
        # form (copy-reduction-http1 P2).
        buf = bytearray(self._request)
        while not buf.endswith(_REQ_END):
            try:
                line = await self._reader.readuntil(b'\r\n')
            except asyncio.LimitOverrunError as exc:
                self._request = bytes(buf)
                raise HeaderTooLargeError(
                    f'asyncio buffer overflow ({exc.consumed} bytes) '
                    f'while reading headers') from exc
            buf += line
            if max_total > 0 and len(buf) > max_total:
                self._request = bytes(buf)
                raise HeaderTooLargeError(
                    f'header block {len(buf)} bytes > '
                    f'BB_HEADER_MAX_TOTAL={max_total}')
        self._request = bytes(buf)

    def _should_keep_alive(self, scope: dict) -> bool:
        """Return True if the connection should persist after this request."""
        http_version = scope.get('http_version', '1.0')
        connection = scope['headers'].get(b'connection', b'').lower()
        if http_version == '1.1':
            return connection != b'close'
        return connection == b'keep-alive'

    async def _handle(self, msg: Message) -> None:  # never reached
        raise NotImplementedError
