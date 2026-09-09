"""HTTP/1.1 Actor classes for the BlackBull actor model.

HTTP1Actor drives the keep-alive loop for one TCP connection.
RequestActor owns the lifetime of a single HTTP request.
"""
import logging
import re
from base64 import b64encode, b64decode
from binascii import Error as BinasciiError
from collections.abc import Awaitable, Callable
from hashlib import sha1
from http import HTTPStatus
from urllib.parse import unquote

from ..actor import Actor, Message
from ..event_aggregator import EventAggregator
from ..asgi import ASGIReceiveCallable, ASGISendCallable
from ..connection import (
    Connection, bind_receive_channel)
from ..headers import Headers
from .deadline import ConnectionDeadline
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
_HTTP_VERSION_RE = re.compile(rb'^HTTP/\d\.\d$')

# The exact negation of the RFC 9110 §5.6.2 tchar set and the §5.5
# CTL-except-HTAB allow-list.  Regexes rather than per-byte membership scans:
# 3–4× faster than the equivalent Python loop per pyperf.
#
# `_FIELD_NAME_INVALID_RE` has no call site here — `_TCHAR_OCTETS` is what
# `_parse` uses — and is **not** dead: `tests/unit/test_parse_octet_tables.py`
# validates that table against it octet for octet, so the fast form cannot
# drift from the RFC without a test failing.
_FIELD_NAME_INVALID_RE = re.compile(rb"[^!#$%&'*+\-.^_`|~0-9A-Za-z]")
_FIELD_VALUE_INVALID_RE = re.compile(rb"[\x00-\x08\x0a-\x1f\x7f]")

# RFC 9110 §5.6.2 tchar, spelled out.
_TCHAR_OCTETS = (b"!#$%&'*+-.^_`|~"
                 b'0123456789'
                 b'ABCDEFGHIJKLMNOPQRSTUVWXYZ'
                 b'abcdefghijklmnopqrstuvwxyz')

_BLOCK_ALLOWED_OCTETS = bytes(
    c for c in range(256)
    if not (c < 0x09 or 0x0B <= c <= 0x0C or 0x0E <= c <= 0x1F or c == 0x7F)
    and c not in (0x0A, 0x0D)
)


def _block_values_are_clean(data: bytes) -> bool:
    """True when no field value in *data* can contain a forbidden octet.

    A ``False`` result never rejects and says nothing about *which* line is at
    fault — it only turns the per-header regex back on, so error messages are
    unchanged.
    """
    residue = data.translate(None, _BLOCK_ALLOWED_OCTETS)
    return residue.count(b'\r\n') * 2 == len(residue)


# The cache key is attacker-controlled, and the resource a peer can force us to
# spend is **bytes**, not entries: an entry-count-only bound would let 7 x 8 KiB
# never-repeating lines per request grow to ~1 MiB per connection, held for as
# long as the peer keeps it alive.  A captured Chromium page load needs 26
# distinct lines totalling 988 B, longest 145 B, so real traffic stays uncapped.

#: Entries.
_LINE_CACHE_MAX = 64

#: Longest line admitted.  1 KiB clears the largest thing a browser really
#: repeats (a ~145 B ``User-Agent``, a session ``Cookie``) by a wide margin.
_LINE_CACHE_MAX_LINE = 1024

#: Total key bytes per connection — the binding constraint, and the one that
#: multiplies by concurrent connections: ~8 KiB/conn worst case (~16 KiB
#: counting the retained name/value slices) against 988 B of real need.
_LINE_CACHE_MAX_BYTES = 8192


# ---------------------------------------------------------------------------
# Shared default line table
# ---------------------------------------------------------------------------
# The admission rule — spec-enumerated values only, and never a framing header
# — is in ``docs/about/internals.md`` §The shared spec table, and pinned by
# ``tests/unit/test_default_line_table.py``.
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
    """Validate every default line and map it to the pair ``_parse`` produces.

    Same expressions, same rules, so a hand-written entry cannot disagree with
    what parsing that line would yield.  A violation raises at import rather
    than serving a wrong pair at runtime.
    """
    table: dict[bytes, tuple[bytes, bytes]] = {}
    for line in _SPEC_ENUMERATED_LINES:
        colon = line.find(b':')
        if colon < 1 or line[0] in (0x20, 0x09):
            raise ValueError(f'malformed default header line: {line!r}')
        key = line[:colon]
        if key.translate(None, _TCHAR_OCTETS):
            raise ValueError(f'invalid name in default header line: {line!r}')
        lkey = key.lower()
        if lkey in _UNDERSCORE_FRAMING_NAMES or lkey in _FRAMING_NAMES:
            raise ValueError(
                f'framing header must not be pre-seeded: {line!r}')
        value = line[colon + 1:].strip(b' \t')
        if _FIELD_VALUE_INVALID_RE.search(value):
            raise ValueError(f'CTL in default header value: {line!r}')
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

# RFC 9110 §8.6 — at most one canonical leading SP (the byte after the colon),
# then ``0`` or a no-leading-zero decimal.  Matched against the *raw*
# post-colon bytes, before the generic OWS strip discards the evidence: leading
# zeros, doubled/tab OWS and trailing OWS are all parser-disagreement smuggling
# vectors (SMUG-CL-LEADING-ZEROS / -DOUBLE-ZERO / -TRAILING-SPACE /
# -EXTRA-LEADING-SP, MAL-CL-TAB-BEFORE-VALUE) a lenient ``int()`` would accept.
_CL_STRICT_RE = re.compile(rb'\A ?(?:0|[1-9][0-9]*)\Z')

# NORM-UNDERSCORE-CL / -TE — header names that differ from a framing
# header only by ``_`` vs ``-``.  Underscore is a legal tchar, but these
# two exist solely to desync a front-end that normalises ``_`` to ``-``
# (CGI-style); nginx drops them by default, we reject.
_UNDERSCORE_FRAMING_NAMES = frozenset((
    b'content_length', b'transfer_encoding'))

_DEFAULT_LINES = _build_default_lines()


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


class UnsupportedVersionError(Exception):
    """RFC 9110 §15.6.6 — the request-line carried a well-formed
    ``HTTP/x.y`` version whose major version the server does not support
    (RFC9112-2.3-INVALID-VERSION).  Answered with 505 HTTP Version Not
    Supported and the connection is closed.  Distinct from
    :class:`BadRequestError`: the request *grammar* was valid."""


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

    Assumes :func:`_validate_message_framing` has already run, so CL/TE
    conflicts and malformed values cannot reach here — a bare ``chunked`` or a
    single well-formed ``Content-Length`` is all that is left to classify.
    """
    if headers.getlist(b'transfer-encoding'):
        return True
    cl = headers.get(b'content-length', b'').strip()
    # ``Content-Length: 0`` — and ``000`` — declares no octets, so there is
    # nothing that could be framed two ways.
    return bool(cl) and bool(cl.lstrip(b'0'))


def _validate_message_framing(headers: 'Headers') -> int:
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
      :class:`NotImplementedFramingError`.

    Returns the declared body length — the validated ``Content-Length``, or 0
    when the message declares none (``chunked`` included: that framing
    announces no total, so its body is counted as it arrives instead).
    Returned rather than looked up again because the common request carries no
    ``Content-Length`` at all, making a second lookup a guaranteed index miss
    plus the fallback probe's ``bytes.lower()`` allocation, on the per-request
    path.
    """
    cls = headers.getlist(b'content-length')
    tes = headers.getlist(b'transfer-encoding')

    if cls and tes:
        raise BadRequestError(
            'Content-Length and Transfer-Encoding both present '
            '(smuggling vector)')

    declared = 0
    if cls:
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
        declared = int(next(iter(values)))

    if tes:
        codings = [c.strip().lower()
                   for _, raw_value in tes for c in raw_value.split(b',')]
        if codings == [b'chunked']:
            pass  # RFC 9112 §6.1 — the one accepted form
        elif b'chunked' not in codings:
            # A coding we don't implement, chunked absent (``gzip``,
            # ``deflate``) ⇒ 501 (nginx parity).
            raise NotImplementedFramingError(
                f'Transfer-Encoding {codings!r} is not implemented')
        elif codings[-1] != b'chunked' or codings.count(b'chunked') > 1:
            # chunked present but not the sole final coding (``chunked, gzip``,
            # ``chunked, chunked``) ⇒ the message length is undeterminable, and
            # a server MUST NOT process it (SMUG-TE-NOT-FINAL-CHUNKED).
            raise BadRequestError(
                f'Transfer-Encoding with chunked not the sole final coding: '
                f'{codings!r}')
        else:
            # chunked IS final but preceded by a coding we can't decode
            # (``gzip, chunked``) ⇒ 501.
            raise NotImplementedFramingError(
                f'Transfer-Encoding {codings!r} applies an unimplemented '
                f'content coding before chunked')

    return declared


# RFC 3986 §3.2 — authority = [userinfo "@"] host [":" port].  None of these
# delimiters belong in a Host value; their presence (or an empty value) is a
# smuggling / SSRF vector nginx rejects with 400 and a lenient parser accepts
# silently.  ``@`` is included: the deprecated userinfo component has no place
# in a Host header and enables credential-spoofing.
_HOST_FORBIDDEN_BYTES = frozenset(b'/?# \t@')
_HOST_FORBIDDEN_RE = re.compile(
    b'[' + re.escape(bytes(sorted(_HOST_FORBIDDEN_BYTES))) + b']')

# RFC 9112 §2.1 / RFC 3986 — a request-target may carry only visible ASCII.
_TARGET_ALLOWED_OCTETS = bytes(range(0x21, 0x7F))


def _parse_host_header(value: bytes, default_port: int) -> tuple[str, int]:
    """Split a Host header value into ``(host, port)``.

    Handles the RFC 3986 §3.2.2 IPv6 bracket form ``[::1]:8100``, where a naive
    ``value.split(b':')`` yields ``int(b'')`` → ``ValueError``.  A missing or
    non-numeric port falls back to *default_port*.
    """
    # ``_validate_host`` rejects non-ASCII on the request path; ``replace``
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
    host, sep, port_s = value.rpartition(b':')
    if sep and port_s.isdigit():
        return _dec(host), int(port_s)
    return _dec(value), default_port


def _validate_host(headers: 'Headers') -> None:
    """RFC 9112 §3.2 / §7.2 — Host MUST be present and contain a valid
    URI-authority component.  Inputs such as ``host: 0/0`` and an empty
    host are accepted by a lenient parser and rejected with 400 by nginx;
    this check keeps BlackBull on the RFC side of that split.
    """
    hosts = headers.getlist(b'host')
    if len(hosts) > 1:
        raise BadRequestError(
            f'multiple Host headers ({len(hosts)} — smuggling vector)')
    if not hosts:
        # The version-aware presence rule lives in ``_parse``, which knows the
        # request version; this helper only grades a value that is present.
        return
    value = hosts[0][1].strip(b' \t')
    if not value:
        raise BadRequestError('empty Host header value')
    if _HOST_FORBIDDEN_RE.search(value):
        raise BadRequestError(
            f'invalid Host authority {value!r}: contains '
            f'delimiter / whitespace forbidden by RFC 3986 §3.2')
    # RFC 3986 §3.2 authorities are ASCII — an internationalised name reaches
    # the wire as punycode, which is too.  Neither check above excludes a high
    # byte: the delimiter set is `/ ? #` plus whitespace, and the CTL check
    # covers \x00-\x08\x0a-\x1f\x7f.  nginx answers 400, and *some* answer is
    # the point — a caller cannot tell a silent drop from a crash.
    try:
        value.decode('ascii')
    except UnicodeDecodeError:
        raise BadRequestError(
            f'invalid Host authority {value!r}: non-ASCII byte in a '
            f'URI authority (RFC 3986 §3.2)') from None


# ---------------------------------------------------------------------------
# RequestActor — single HTTP request lifetime
# ---------------------------------------------------------------------------

class RequestActor(Actor):
    """Owns one request's app boundary: what the app is called with.

    Shared by both protocol actors — H/1 reuses one instance per connection
    via :meth:`bind`; H/2 builds one per stream.  Owns the app-facing
    representation (the native :class:`Connection` on the default lane, the
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
        """Point this actor at the next request on the same connection.

        HTTP/1.1 dispatches one request at a time per connection, so the
        instance is free between requests and rebinding it is indistinguishable
        from building a new one — except for the allocation, which the keep-alive
        loop would otherwise pay on every request.  ``app``, ``aggregator`` and
        ``force_asgi`` are per-connection and stay put.

        Deliberately **not** available to HTTP/2, whose streams are concurrent:
        two live requests sharing one actor would interleave their fields.
        """
        self._conn = conn
        self._recipient = recipient
        self._send = send
        return self

    async def run(self) -> None:  # override: single-shot, no inbox loop
        # Inlined rather than a helper call: the per-request hot path, where an
        # extra call frame measured ~0.1-0.2 %.
        if self._force_asgi:
            target = self._conn.to_asgi_scope(force_asgi=True)
        else:
            target = self._conn
        # Bind the *raw* recipient, before any disconnect-detecting wrapper
        # exists: binding the wrapper closes a per-request reference cycle
        # (target._receive → wrapper → target) reclaimable only by the cyclic
        # GC, and cost tail latency.  Idempotent.
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

    If *aggregator* is ``None`` the actor falls back to the legacy direct-
    dispatcher path (fires events via ``app._dispatcher`` directly), so that
    BlackBull apps without a full EventAggregator still receive lifecycle events.
    """

    # Class-level defaults, because test doubles built with ``object.__new__``
    # never run ``__init__``.  Immutable ones only: a mutable class default is
    # shared by *every* connection, which for the line cache is precisely the
    # cross-connection bleed its design forbids.
    _ssl: bool = False
    _line_cache: 'dict[bytes, tuple[bytes, bytes]] | None' = None
    _line_cache_bytes: int = 0
    _max_line: int | None = None
    #: Body length the current request declares, as validated by
    #: :func:`_validate_message_framing`; 0 when it declares none.
    _declared_body_len: int = 0

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
        import asyncio  # noqa: PLC0415
        import time as _time  # noqa: PLC0415
        from ..env import get_settings as _get_settings  # noqa: PLC0415
        cfg = _get_settings()
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

                # RFC 9110 §15.5.14 — refused before a single octet is read.
                # The per-read bound (``body_chunk_max``) caps what one read
                # materialises, never the sum, so without this the peer picks
                # how much memory the request costs.  Answered ahead of any
                # ``Expect: 100-continue`` for the reason the status exists
                # (RFC 9110 §10.1.1 — a final status tells the peer not to send
                # the body), and the connection closes because the refused
                # octets are still coming: reading the next request out of them
                # is the smuggling shape.
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

                # Loop tail, and no per-path exemption — see
                # ``docs/about/internals.md`` §Keep-alive drain invariant.
                # One verdict rather than two predicates: whether the message
                # boundary survived, and so what this connection may do next, is
                # the recipient's judgement, and asking ``must_close`` and then
                # ``needs_drain()`` left the two free to drift apart.
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
        """Parse raw HTTP/1.1 request bytes into a native :class:`Connection`.

        Raises :class:`BadRequestError` on an RFC 9112 framing violation the
        caller should answer with 400, and :class:`HeaderTooLargeError` when a
        single line exceeds ``BB_HEADER_MAX_LINE``.  The whole-block limit
        (``BB_HEADER_MAX_TOTAL``) is enforced in ``run()``, which sees the
        accumulating buffer; per-line is cheaper here, post-split.
        """
        # Memoised per connection.  The cost removed is not ``get_settings()``
        # (it is ``functools.cache``d) but the ``from ..env import`` statement
        # that would run ahead of it on every single parse.
        max_line = self._max_line
        if max_line is None:
            from ..env import get_settings as _get_settings  # noqa: PLC0415
            max_line = self._max_line = _get_settings().header_max_line
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

        # Method (§4 / RFC 9110 §9.1) — case-sensitive token of 1+ tchar.
        if not method or method.translate(None, _TCHAR_OCTETS):
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
        if method == b'CONNECT':
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
        elif (_ss := path.find(b'://')) != -1 and b'/' not in path[:_ss]:
            # absolute-form (§3.2.2): ``scheme "://" authority path-abempty``.
            # Rewrite it to origin-form and let the authority override Host
            # (§3.2.2 — the origin server MUST ignore the Host header here).
            rest = path[_ss + 3:]
            slash = rest.find(b'/')
            if slash == -1:
                authority_override, path = rest, b'/'
            else:
                authority_override, path = rest[:slash], rest[slash:]
            if not authority_override:
                raise BadRequestError(
                    f'absolute-form request-target has empty authority: '
                    f'{path!r}')

        # Request-target octets — reject CTLs, DEL, and non-ASCII (§2.1 /
        # RFC 3986: a raw byte ≥ 0x80 in the target is a normalisation /
        # smuggling vector, MAL-NON-ASCII-URL).  Skipped for asterisk-form
        # (the literal ``*`` is validated above).
        if not asterisk_form and (
                not path or path.translate(None, _TARGET_ALLOWED_OCTETS)):
            raise BadRequestError(f'invalid request-target {path!r}')

        if asterisk_form:
            _raw_path_b, _query_string = b'*', b''
        else:
            # C-level partition calls (~12× faster than urlparse): strip
            # #fragment, then split ?query.  ';' is NOT split off — RFC 3986
            # makes it an ordinary path sub-delimiter (the ;params grammar is
            # obsolete RFC 2396), so it stays in the path component.
            _no_frag, _, _ = path.partition(b'#')
            _raw_path_b, _, _query_string = _no_frag.partition(b'?')

        # The b'%' guard keeps the no-escape case on the plain-decode path;
        # the target was already rejected above if it holds a byte >= 0x80, so
        # ``decode('ascii')`` cannot fail.  unquote semantics match uvicorn:
        # '+' stays literal, malformed escapes pass through, never raises.
        if b'%' in _raw_path_b:
            _decoded_path = unquote(_raw_path_b.decode('ascii'),
                                  encoding='utf-8', errors='replace')
        else:
            _decoded_path = _raw_path_b.decode('utf-8')

        values_need_checking = not _block_values_are_clean(data)

        cache = self._line_cache
        if cache is None:
            cache = self._line_cache = {}
        # A probe into a cache known to be empty buys nothing, and on a
        # connection's first request that is every probe.  Decided once per
        # request, not once per line.
        do_lookup = len(cache) > 0

        raw: list[tuple[bytes, bytes]] = []
        for line in lines[idx + 1:]:
            if not line:
                # Empty line = end of headers; anything after is body (already
                # split off upstream because we read until CRLFCRLF).
                continue
            # Too long to be admitted ⇒ skip the cache entirely, the lookup
            # included: hashing 8 KiB for an answer that is always "no" is the
            # adversary's cheapest way to spend our CPU.
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
                    continue
            # RFC 9112 §5.2 — obs-fold MUST be rejected in requests.  Indexing
            # skips the one-byte slice a `line[:1]` comparison would allocate;
            # the empty-line case is retired by the `continue` above.
            if line[0] in (0x20, 0x09):
                raise BadRequestError(
                    f'obsolete line folding rejected: {line!r}')
            colon = line.find(b':')
            if colon < 1:
                raise BadRequestError(f'malformed header line: {line!r}')
            key = line[:colon]
            value = line[colon + 1:]
            # field-name must be a valid token (§5.1 / RFC 9110 §5.6.2).  SP
            # and HTAB are not tchar, so this one test also decides §5.1 (no
            # whitespace between field-name and ':'), and only a rejected name
            # pays to tell the two apart.  `colon < 1` makes `key[-1]` safe.
            if key.translate(None, _TCHAR_OCTETS):
                if key[-1] in (0x20, 0x09):
                    raise BadRequestError(
                        f'whitespace before colon (smuggling vector): {line!r}')
                raise BadRequestError(f'invalid header name {key!r}')
            lkey = key.lower()
            if lkey in _UNDERSCORE_FRAMING_NAMES:
                raise BadRequestError(
                    f'framing-confusable header name {key!r} '
                    f'(NORM-UNDERSCORE)')
            if lkey == b'content-length' and not _CL_STRICT_RE.match(value):
                raise BadRequestError(
                    f'ambiguous Content-Length value {value!r} '
                    f'(RFC 9110 §8.6)')
            # Strip the OWS surrounding the value (§5).
            value = value.strip(b' \t')
            if values_need_checking and _FIELD_VALUE_INVALID_RE.search(value):
                raise BadRequestError(
                    f'CTL in header value (smuggling / log-injection): '
                    f'{key!r}: {value!r}')
            pair = (lkey, value)
            raw.append(pair)
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
        # Names were lowercased in the loop above while being validated;
        # `Headers.__init__` would lowercase them a second time.
        headers = Headers.from_lowered(raw)

        # RFC 9110 §8.3 — Content-Type is a singleton; multiple values are
        # ambiguous and a request-smuggling surface (COMP-DUPLICATE-CT).
        if len(headers.getlist(b'content-type')) > 1:
            raise BadRequestError('multiple Content-Type headers')

        # RFC 9112 §6 — framing rejected before any body byte is read.  ``run``
        # weighs the returned length against ``BB_MAX_BODY_SIZE``.
        self._declared_body_len = _validate_message_framing(headers)
        _validate_host(headers)
        # RFC 9112 §3.2 / §7.2 — every HTTP/1.1 (and later 1.x) request MUST
        # carry a Host header (RFC9112-7.1-MISSING-HOST); only HTTP/1.0, which
        # predates Host, may omit it (COMP-HTTP10-NO-HOST).
        if version != b'HTTP/1.0' and not headers.getlist(b'host'):
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

        if headers.getlist(b'host'):
            default_port = _HTTPS_PORT if self._ssl else _HTTP_PORT
            host, port = _parse_host_header(headers.get(b'host'), default_port)
            conn.server = (host, port)

        if headers.getlist(b'upgrade'):
            # RFC 9110 §7.8 — a server MAY ignore an Upgrade it does not
            # support and MUST NOT fail the request over it.  Only WebSocket
            # may switch ``conn.type``; any other token (notably curl's default
            # ``Upgrade: h2c`` probe on ``--http2``) is served as ordinary
            # HTTP/1.1, because dispatch has no route for it and the connection
            # would close with no reply.
            if headers.get(b'upgrade').strip().lower() == b'websocket':
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
        key = headers.get(b'sec-websocket-key', b'').strip()
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
        from ..env import get_settings as _get_settings  # noqa: PLC0415
        from .permessage_deflate import negotiate as _negotiate_deflate  # noqa: PLC0415
        deflate_params = None
        deflate_response = None
        if _get_settings().ws_permessage_deflate:
            offer = headers.get(b'sec-websocket-extensions', b'')
            deflate_params, deflate_response = _negotiate_deflate(offer or None)

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
        """Prepare and run one request; return ``(keep_alive, inner_receive)``.

        Protocol-side preparation only: the access-log record, the sender's
        per-request reset, the Expect/100-continue answer, the HEAD→GET
        rewrite, and the recipient.  Everything app-facing is delegated to
        :class:`RequestActor`, the shared app boundary.

        The HEAD→GET rewrite must run before :class:`RequestActor` snapshots
        the app argument; reversed, the compat lane freezes ``method='HEAD'``,
        the router finds no HEAD route, and the dual-path lane answers 405
        where the native lane answers 200 (COMP-HEAD-NO-BODY).
        ``test_head_dual_path.py`` is the guard.
        """
        import asyncio  # noqa: PLC0415

        # Only when something consumes it (access log / phase trace /
        # request_completed listener).  Otherwise ``None``, skipping a
        # per-request allocation and the ``conn.state`` dict it forces — the
        # Connection graph's per-request objects are what the cyclic GC scans.
        if _request_record_needed(self._aggregator):
            log_record = _AccessLogRecord.from_conn(conn)
            if _PHASE_TRACE:
                log_record.phases['loop_start'] = (
                    loop_start_perf, loop_start_cpu)
            log_record.mark('parsed')
            conn.state['access_log'] = log_record
        else:
            log_record = None

        # The sender is shared across keep-alive requests: without the reset
        # ``_started`` stays True after the first response, and the timeout
        # branch's ``if not send._started`` then skips the synthetic 408 on a
        # second-or-later request.  ``_chunked`` / ``_buffered_status``
        # likewise outlive their request.
        send.reset_per_request_state()

        # RFC 9110 §10.1.1 / §15.2 — MUST NOT send a 1xx to an HTTP/1.0 client
        # (COMP-NO-1XX-HTTP10); the Expect header is ignored.
        #
        # Placed after the reset and before the sender capture below.  Written
        # before the reset, the previous response's "already complete" guard
        # drops the interim response from request two onward and the peer
        # stalls until its own Expect timeout; written after the capture, the
        # interim status lands in the record the real response owns.
        if (conn.http_version != '1.0'
                and conn.headers.get(b'expect').lower() == b'100-continue'):
            await send(b'', HTTPStatus.CONTINUE)

        # Inline access-log capture into the sender — avoids the per-event
        # coroutine dispatch through a wrapper (7% of CPU in the py-spy
        # profile).  RFC 9110 §9.3.2 — a HEAD response is the GET response
        # without the body, synthesised by dispatching to the GET handler and
        # stripping body bytes.  The rewrite lands on the Connection only: a
        # materialized scope reads ``method`` back from it, so writing
        # ``scope['method']`` too would force materialization for nothing.
        # The access log keeps the original HEAD, from the request line.
        send._log_record = log_record
        send._head_mode = (conn.method == 'HEAD')
        if send._head_mode:
            conn.method = 'GET'

        # Reusable: reader, body timeout and deadline are all connection
        # properties.  ``bind`` re-derives the framing from the new head.
        first_request_on_connection = inner_receive is None
        if first_request_on_connection:
            inner_receive = RecipientFactory.http1(
                self._reader, conn, body_timeout=cfg.body_timeout, deadline=dl)
        else:
            inner_receive.bind(conn)

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
        connection = conn.headers.get(b'connection', b'').lower()
        if http_version == '1.1':
            return connection != b'close'
        return connection == b'keep-alive'

    async def _handle(self, msg: Message) -> None:  # never reached
        raise NotImplementedError
