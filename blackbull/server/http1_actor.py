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
from ..event import Event
from ..event_aggregator import EventAggregator
from ..asgi import ASGIEvent, ASGIReceiveCallable, ASGISendCallable
from ..connection import (
    Connection, bind_receive_channel, disconnected, mark_disconnected)
from ..headers import Headers
from .deadline import ConnectionDeadline
from .connection_protocol import HeadTooLargeError
from .recipient import (AbstractReader, HTTP1Recipient, IncompleteReadError,
                        RecipientFactory, _WS_READ_INLINE)
from .sender import AbstractWriter, SenderFactory
from .access_log import (AccessLogRecord as _AccessLogRecord,
                         _make_disconnect_detecting_receive,
                         emit_access_log as _emit_access_log,
                         request_record_needed as _request_record_needed,
                         disconnect_events_observed as _disconnect_events_observed,
                         PHASE_TRACE as _PHASE_TRACE)
from .cap_log import log_cap_hit

logger = logging.getLogger(__name__)

_REQ_END = b'\r\n\r\n'
_WS_GUID = b'258EAFA5-E914-47DA-95CA-C5AB0DC85B11'  # RFC 6455 §1.3
# Methods advertised in the Allow header of a server-wide ``OPTIONS *``
# response (RFC 9112 §3.2.4).  The origin implements the standard set; route
# dispatch is bypassed for asterisk-form, so this is a server-level answer.
_SERVER_WIDE_ALLOW = b'GET, HEAD, POST, PUT, PATCH, DELETE, OPTIONS'
_HTTP_PORT  = 80
_HTTPS_PORT = 443

# Upper bound on request-body bytes drained to recover keep-alive framing
# when a handler leaves the body unread.  A larger unread body
# closes the connection rather than spending bandwidth draining it — nginx's
# lingering-close does the same.
_MAX_KEEPALIVE_DRAIN = 64 * 1024

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
_HTTP_VERSION_RE = re.compile(rb'^HTTP/\d\.\d$')

# Compiled-regex validators replace the per-byte membership scans this parser
# used to run.  The character classes below are the exact negation of the RFC
# 9110 §5.6.2 tchar set and the RFC 9110 §5.5 CTL-except-HTAB allow-list
# respectively; `re.search` returns a Match on the first invalid byte (3–4×
# faster than the equivalent Python loop per pyperf).
#
# `_FIELD_NAME_INVALID_RE` has no call site in this module — `_TCHAR_OCTETS`
# below is what `_parse` uses — but it is **not** dead: it is the reference
# `tests/unit/test_parse_octet_tables.py` validates that table against, octet
# for octet, so the fast form cannot drift from the RFC without a test failing.
_FIELD_NAME_INVALID_RE = re.compile(rb"[^!#$%&'*+\-.^_`|~0-9A-Za-z]")
_FIELD_VALUE_INVALID_RE = re.compile(rb"[\x00-\x08\x0a-\x1f\x7f]")

# RFC 9110 §5.6.2 tchar, spelled out.  Deleting every allowed octet leaves
# the empty string for a valid token, so a non-empty result is the rejection
# — the same trick as `_TARGET_ALLOWED_OCTETS`, and measurably cheaper than
# `_FIELD_NAME_INVALID_RE` (which stays as the source of truth this table is
# tested against, octet for octet).
_TCHAR_OCTETS = (b"!#$%&'*+-.^_`|~"
                 b'0123456789'
                 b'ABCDEFGHIJKLMNOPQRSTUVWXYZ'
                 b'abcdefghijklmnopqrstuvwxyz')

# Everything a header *block* may contain, so that deleting it leaves only
# CR, LF, and the CTLs a field value may not carry (`_FIELD_VALUE_INVALID_RE`).
# A clean block therefore reduces to a run of CRLFs — which is what
# `_block_values_are_clean` checks, in one C-level pass for the whole request
# instead of one regex per header.
_BLOCK_ALLOWED_OCTETS = bytes(
    c for c in range(256)
    if not (c < 0x09 or 0x0B <= c <= 0x0C or 0x0E <= c <= 0x1F or c == 0x7F)
    and c not in (0x0A, 0x0D)
)


def _block_values_are_clean(data: bytes) -> bool:
    """True when no field value in *data* can contain a forbidden octet.

    Deleting every permitted octet leaves only CR, LF and forbidden CTLs.
    If what remains tiles exactly into CRLF pairs, then every CR and LF in
    the block is a line terminator and no forbidden CTL appears anywhere —
    so the per-header field-value check cannot fire, and is skipped.

    A ``False`` result proves nothing about *which* line is at fault (it can
    even be the request line, which has its own checks), so it only turns the
    per-header regex back on.  The caller's error messages are unchanged.
    """
    residue = data.translate(None, _BLOCK_ALLOWED_OCTETS)
    return residue.count(b'\r\n') * 2 == len(residue)


# Bounds on one connection's validated header-line cache.  A keep-alive peer
# resends byte-identical lines (``User-Agent``, ``Accept``, ``Cookie`` — real
# browsers do this), so the split/lower/validate work for each is done once per
# connection instead of once per request.
#
# All three bounds exist because **the cache key is attacker-controlled**, and
# the resource a peer can force us to spend is *bytes*, not entries.  A captured
# Chromium page load needs 26 distinct lines totalling 988 B, longest 145 B —
# so these leave real traffic entirely uncapped while denying the adversarial
# shape (7 x 8 KiB never-repeating lines per request, which an entry-count-only
# bound would let grow to ~1 MiB per connection, held for as long as the peer
# keeps the connection alive).

#: Entries.  Bounds the dict itself.
_LINE_CACHE_MAX = 64

#: Longest line admitted.  A line above this skips the cache **entirely** — no
#: lookup either, so a huge line never pays a hash it cannot benefit from.
#: 1 KiB clears the largest thing a browser really repeats (a ~145 B
#: ``User-Agent``, a session ``Cookie``) by a wide margin.
_LINE_CACHE_MAX_LINE = 1024

#: Total key bytes admitted per connection.  The binding constraint, and the
#: one that multiplies by concurrent connections: ~8 KiB/conn worst case
#: (~16 KiB counting the retained name/value slices) against 988 B of real
#: need.
_LINE_CACHE_MAX_BYTES = 8192


# ---------------------------------------------------------------------------
# Shared default line table
# ---------------------------------------------------------------------------
# The per-connection cache cannot help the *first* request on a connection —
# there is nothing in it yet — which is the whole of the interaction for a
# ``Connection: close`` client.  This table closes that gap: header lines whose
# value set is fixed by a specification are the same bytes for every client on
# every deployment, so they can be validated once at import and shared.
#
# **The admission rule is what keeps this honest**: a line belongs here only if
# its value set is enumerated by a spec — Fetch Metadata's ``Sec-Fetch-*``,
# the UA client hints' boolean/platform forms, and the fixed tokens of RFC 9110
# — plus a handful of genuinely universal idioms.  Values *observed in a packet
# capture* do not qualify, however frequent: seeding the table from a capture
# would be tuning the server to the browser that happened to be measured.
# That is why no ``User-Agent``, ``Accept-Language``, ``sec-ch-ua``, ``Host``,
# ``Referer`` or ``Cookie`` line appears below, though all are frequent.
#
# Framing-relevant names (``Content-Length``, ``Transfer-Encoding``, ``Host``,
# ``Connection`` values that change framing semantics beyond the two tokens
# below) are deliberately absent: a shared table is the last place a framing
# decision should come from.  ``tests/unit/test_default_line_table.py`` pins
# both rules.
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
    # Universal idioms rather than spec enums, and few on purpose: ``*/*`` is
    # what every non-browser client sends, and the gzip/deflate/br/zstd
    # progression is the content-coding registry in the order it grew.
    b'Accept: */*',
    b'Accept-Encoding: gzip',
    b'Accept-Encoding: gzip, deflate',
    b'Accept-Encoding: gzip, deflate, br',
    b'Accept-Encoding: gzip, deflate, br, zstd',
    b'Accept-Encoding: identity',
)


def _build_default_lines() -> dict[bytes, tuple[bytes, bytes]]:
    """Validate every default line and map it to the pair ``_parse`` produces.

    The pair is derived with the *same* expressions the parse loop uses and
    checked with the *same* rules, so a hand-written entry cannot disagree with
    what parsing that line would have yielded.  A violation raises at import
    rather than serving a wrong pair at runtime.
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


#: Names a shared table must never carry, because they decide message framing
#: or routing and must be read from the request every time.
_FRAMING_NAMES = frozenset({
    b'content-length', b'transfer-encoding', b'host', b'expect', b'upgrade',
    b'trailer', b'te-framing',
})

# Built at the bottom of this constant block: ``_build_default_lines`` runs the
# real validation rules, and ``_UNDERSCORE_FRAMING_NAMES`` is defined below.

# RFC 9110 §8.6 — strict Content-Length: at most one canonical leading SP
# (the byte after the colon), then ``0`` or a no-leading-zero decimal.
# Matched against the *raw* post-colon bytes, before the generic OWS strip
# discards the evidence: leading zeros, doubled/tab OWS, and trailing OWS
# are all parser-disagreement smuggling vectors (SMUG-CL-LEADING-ZEROS /
# -DOUBLE-ZERO / -TRAILING-SPACE / -EXTRA-LEADING-SP,
# MAL-CL-TAB-BEFORE-VALUE) that a lenient ``int()`` would silently accept.
_CL_STRICT_RE = re.compile(rb'\A ?(?:0|[1-9][0-9]*)\Z')

# NORM-UNDERSCORE-CL / -TE — header names that differ from a framing
# header only by ``_`` vs ``-``.  Underscore is a legal tchar, but these
# two exist solely to desync a front-end that normalises ``_`` to ``-``
# (CGI-style); nginx drops them by default, we reject.
_UNDERSCORE_FRAMING_NAMES = frozenset((
    b'content_length', b'transfer_encoding'))

#: Built here rather than beside its literals because validating an entry needs
#: every rule above, ``_UNDERSCORE_FRAMING_NAMES`` included.  An invalid or
#: framing-relevant entry raises at import — the table can never be wrong at
#: runtime, only absent.
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

    Two RFC rules can be broken by the same overrun, and they get different
    answers.  If the *first* CRLF lands beyond the budget the start-line never
    ended: that is a malformed request-line (RFC 9112 §3) and the answer is
    **400** — a 100 KiB method token is not "too many header fields".  When the
    lines are well-formed and merely numerous, it is **431** (RFC 6585 §5).

    Always raises.
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
    # ``Content-Length: 0`` declares an empty body: no octets, nothing to
    # frame two ways.  ``lstrip(b'0')`` because "000" is also zero.
    return bool(cl) and bool(cl.lstrip(b'0'))


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
      connection dropped silently.
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

    # Transfer-Encoding validation.
    #
    # RFC 9112 §6.1 distinguishes two failure modes:
    #   * ``chunked`` present but NOT the final coding (``chunked, gzip``,
    #     ``chunked, chunked``) ⇒ the message length is undeterminable ⇒
    #     **400 Bad Request** (SMUG-TE-NOT-FINAL-CHUNKED).  A server MUST NOT
    #     process such a message.
    #   * a coding we don't implement where chunked is absent (``gzip``,
    #     ``deflate``) ⇒ **501 Not Implemented** (nginx parity).
    # We accept exactly a single bare ``chunked`` token.
    if tes:
        # Flatten comma-combined + multi-header codings, in order.
        codings = [c.strip().lower()
                   for _, raw_value in tes for c in raw_value.split(b',')]
        if codings == [b'chunked']:
            pass  # the one accepted form
        elif b'chunked' not in codings:
            # No chunked at all (``gzip``, ``deflate``) ⇒ a coding we don't
            # implement ⇒ 501 (nginx parity).
            raise NotImplementedFramingError(
                f'Transfer-Encoding {codings!r} is not implemented')
        elif codings[-1] != b'chunked' or codings.count(b'chunked') > 1:
            # chunked present but not the sole final coding (``chunked, gzip``,
            # ``chunked, chunked``) ⇒ message length undeterminable ⇒ 400.
            raise BadRequestError(
                f'Transfer-Encoding with chunked not the sole final coding: '
                f'{codings!r}')
        else:
            # chunked IS final but preceded by a coding we can't decode
            # (``gzip, chunked``) ⇒ 501.
            raise NotImplementedFramingError(
                f'Transfer-Encoding {codings!r} applies an unimplemented '
                f'content coding before chunked')


# RFC 3986 §3.2 — authority = [userinfo "@"] host [":" port].  None of
# these delimiter octets belong in a Host header value; their presence
# (or an empty value) is a smuggling / SSRF vector that nginx rejects
# with 400 and a lenient parser accepts silently.  ``@`` is
# included: the deprecated userinfo
# component has no place in a Host header and enables credential-spoofing.
_HOST_FORBIDDEN_BYTES = frozenset(b'/?# \t@')
# Derived from the set above so the two cannot drift.  A small forbidden
# class scans faster as a regex than as a `translate` delete table (the
# opposite of the request-target case below, where the allowed set is large).
_HOST_FORBIDDEN_RE = re.compile(
    b'[' + re.escape(bytes(sorted(_HOST_FORBIDDEN_BYTES))) + b']')

# RFC 9112 §2.1 / RFC 3986 — a request-target may carry only visible ASCII.
# Deleting every allowed octet leaves the empty string for a valid target, so
# a non-empty result *is* the rejection: one C-level pass instead of a Python
# generator over each byte, and no allocation in the accept case (CPython
# returns the shared empty bytes).
_TARGET_ALLOWED_OCTETS = bytes(range(0x21, 0x7F))


def _parse_host_header(value: bytes, default_port: int) -> tuple[str, int]:
    """Split a Host header value into ``(host, port)``.

    Handles the RFC 3986 §3.2.2 IPv6 bracket form ``[::1]:8100`` — a
    naive ``value.split(b':')`` yields ``int(b'')`` → ``ValueError`` on
    those (``b'[::1]:8100'.split(b':')`` == ``[b'[', b'', b'1]', b'8100']``).
    Pre-fix that exception bubbled past ``HTTP1Actor.run`` and closed the
    transport with no response bytes — every IPv6 request saw an "empty
    reply from server".

    A missing or non-numeric port falls back to *default_port*.
    """
    if value.startswith(b'['):
        end = value.find(b']')
        if end != -1:
            host = value[1:end]
            rest = value[end + 1:]
            if rest.startswith(b':') and rest[1:].isdigit():
                return host.decode('utf-8'), int(rest[1:])
            return host.decode('utf-8'), default_port
        # Unterminated bracket — treat the whole value as the host.
        return value.decode('utf-8'), default_port
    host, sep, port_s = value.rpartition(b':')
    if sep and port_s.isdigit():
        return host.decode('utf-8'), int(port_s)
    return value.decode('utf-8'), default_port


def _validate_host(headers: 'Headers') -> None:
    """RFC 9112 §3.2 / §7.2 — Host MUST be present and contain a valid
    URI-authority component.  Inputs such as ``host: 0/0`` and an empty
    host are accepted by a lenient parser and rejected with 400 by nginx;
    this check keeps BlackBull on the RFC side of that split.

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
        # HTTP/1.1 §3.2 requires Host but HTTP/1.0 doesn't.  The
        # version-aware presence check lives in ``_parse`` (which knows
        # the request version); this helper only validates the value's
        # grammar when a Host is present.
        return
    value = hosts[0][1].strip(b' \t')
    if not value:
        raise BadRequestError('empty Host header value')
    if _HOST_FORBIDDEN_RE.search(value):
        raise BadRequestError(
            f'invalid Host authority {value!r}: contains '
            f'delimiter / whitespace forbidden by RFC 3986 §3.2')


# ---------------------------------------------------------------------------
# RequestActor — single HTTP request lifetime
# ---------------------------------------------------------------------------

class RequestActor(Actor):
    """Owns one HTTP/1.1 request lifetime.

    Spawned by HTTP1Actor, awaited to completion.  Calls the ASGI app.

    The request-lifecycle Level B events are emitted by the application
    layer (``BlackBull._dispatch`` / ``__call__``) —
    the cross-transport emission points — not here.  The
    actor layer emits only the Level B ``error`` event, for exceptions that
    escape the app call (e.g. a raising global middleware).
    """

    def __init__(
        self,
        conn: dict | Connection,
        receive: ASGIReceiveCallable,
        send: ASGISendCallable,
        app: Callable[..., Awaitable[None]],
        aggregator: EventAggregator,
    ) -> None:
        super().__init__()
        self._conn = conn
        self._receive = receive
        self._send = send
        self._app = app
        self._aggregator = aggregator

    def bind(self, conn, receive, send) -> 'RequestActor':
        """Point this actor at the next request on the same connection.

        HTTP/1.1 dispatches one request at a time per connection, so the
        instance is free between requests and rebinding it is indistinguishable
        from building a new one — except for the allocation, which the keep-alive
        loop would otherwise pay on every request.  ``app`` and ``aggregator``
        are per-connection and stay put.

        Deliberately **not** available to HTTP/2, whose streams are concurrent:
        two live requests sharing one actor would interleave their fields.
        """
        self._conn = conn
        self._receive = receive
        self._send = send
        return self

    async def run(self) -> None:  # override: single-shot, no inbox loop
        try:
            await self._app(self._conn, self._receive, self._send)
        except BaseException as e:
            await self._aggregator.on_error(self._conn, e)
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

    # Class-level default so tests that instantiate via ``object.__new__``
    # without calling ``__init__`` (test_parser.py uses this pattern to
    # exercise ``_parse`` in isolation) see a cleartext scope by default.
    _ssl: bool = False

    # Both are per-connection state that ``_parse`` memoises on first use.
    # They are declared here as immutable ``None`` rather than initialised in
    # ``__init__`` for two reasons: the ``__new__`` test doubles above never
    # run ``__init__``, and a mutable class-level default would be *shared by
    # every connection* — which for the line cache is precisely the
    # cross-connection bleed its design forbids.
    _line_cache: 'dict[bytes, tuple[bytes, bytes]] | None' = None
    _line_cache_bytes: int = 0
    _max_line: int | None = None

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
        self._reader = reader
        self._writer = writer
        self._app = app
        self._aggregator = aggregator
        self._request = request
        self._peername = peername
        self._sockname = sockname
        self._ssl = ssl
        self._ws_queue_depth = ws_queue_depth
        # Built on the first request, rebound on every later one — see
        # ``RequestActor.bind``.
        self._request_actor: RequestActor | None = None
        # Accept-time connection id (ConnectionActor's) — the one id for the
        # whole connection.  The WS upgrade path reuses it in the scope
        # instead of minting a second one; empty when the actor is
        # constructed directly (tests), in which case the upgrade path
        # falls back to generating a fresh id.
        self._connection_id = connection_id
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
        # One rescheduled TimerHandle per connection drives
        # all phase deadlines (headers / body / keep-alive).  Created
        # lazily so tests that instantiate HTTP1Actor without a wrapping
        # ConnectionActor still work — same task either way.
        if self._deadline is None:
            self._deadline = ConnectionDeadline()
        dl = self._deadline
        send = SenderFactory.http1(self._writer)
        # Capture loop_start at the very top
        # of each iteration so we can quantify the between-request gap
        # (dispatch_done(N) → loop_start(N+1)) on a keep-alive
        # connection.  Plain locals — assigned to log_record.phases
        # below once the record exists.
        _loop_start_perf: float = 0.0
        _loop_start_cpu: float = 0.0
        # Built on the first request that needs one, rebound thereafter.
        inner_receive: HTTP1Recipient | None = None
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
                    # Distinguish idle EOF from
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
                        await self._send_error_and_close(
                            send, b'400 Bad Request', HTTPStatus.BAD_REQUEST)
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
                    await self._send_error_and_close(
                        send, b'431 Request Header Fields Too Large',
                        HTTPStatus.REQUEST_HEADER_FIELDS_TOO_LARGE)
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
                    await self._send_error_and_close(
                        send, b'408 Request Timeout', HTTPStatus.REQUEST_TIMEOUT)
                    return

                try:
                    conn = self._parse(self._request)
                except HeaderTooLargeError as exc:
                    # Per-line limit hit during parse.  Same response as the
                    # total-block check in _read_headers.
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
                    # RFC 9112 §3 / §5 violation — answer with 400 and close.
                    # A malformed request is a smuggling vector candidate, so
                    # we always terminate the connection rather than try to
                    # find the next message boundary.
                    logger.warning('400 Bad Request: %s', exc)
                    await self._send_error_and_close(
                        send, b'400 Bad Request', HTTPStatus.BAD_REQUEST)
                    return
                except NotImplementedFramingError as exc:
                    # RFC 9112 §6.1: server received a
                    # Transfer-Encoding it does not implement.  A lenient
                    # this raised inside HTTP1Recipient and the connection
                    # dropped silently (Finding C in user-corpus).  Match
                    # nginx and answer with 501 then close.
                    logger.warning('501 Not Implemented: %s', exc)
                    await self._send_error_and_close(
                        send, b'501 Not Implemented', HTTPStatus.NOT_IMPLEMENTED)
                    return
                except UnsupportedVersionError as exc:
                    # RFC 9110 §15.6.6 — well-formed request-line, but a
                    # major HTTP version this server does not speak
                    # (RFC9112-2.3-INVALID-VERSION).  505 then close.
                    logger.warning('505 HTTP Version Not Supported: %s', exc)
                    await self._send_error_and_close(
                        send, b'505 HTTP Version Not Supported',
                        HTTPStatus.HTTP_VERSION_NOT_SUPPORTED)
                    return
                self._fill_connection_info(conn)

                # Native-Connection entry: BlackBull's own
                # server dispatches the typed ``Connection`` directly — the app
                # is ``app(conn, receive, send)``, no ASGI scope object at all.
                # Only ``BB_FORCE_ASGI_SCOPE=1`` takes the compat lane: the server
                # converts ``Connection → scope`` (a pure ASGI dict, no stash) and
                # the app converts it back with ``from_scope`` at dispatch — so
                # ``app(scope, …)`` is used *iff* the flag is set. The actor's own
                # plumbing (RecipientFactory, access log, keep-alive) always reads
                # ``conn``.
                if conn.type == 'websocket':
                    # WebSocket is native too: the upgrade path
                    # threads the typed Connection — the WS-only extras
                    # (subprotocols, the deferred 101 responder, deflate params)
                    # live on it (``conn.subprotocols`` / ``conn._ws``), so there
                    # is no scope dict here.
                    await self._handle_upgrade(conn)
                    return

                # BB_REQUEST_TIMEOUT parity with the
                # HTTP/2 path.  ``HTTP2Actor._spawn_stream_task`` wraps each
                # stream coroutine with ``asyncio.wait_for``; the HTTP/1.1
                # path mirrors that here.  On expiry: synthesise 408 if
                # headers haven't shipped yet, then close the connection
                # (no keep-alive across a timed-out request).
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

                # Drain any request body left unread — by a handler that
                # ignores ``receive`` (a POST that 404s), or by a path that
                # answers before routing at all (the server-wide ``OPTIONS *``).
                # Without this the leftover body bytes are parsed as the next
                # pipelined request line — a classic keep-alive framing desync,
                # and behind a connection-pooling reverse proxy the standard
                # request-smuggling shape.  The predicate belongs to the loop
                # tail, not to any one answer path: a per-path exemption is how
                # the ``OPTIONS *`` gap existed, and the next path added would
                # reintroduce it.  A body larger than the drain bound closes the
                # connection instead, so an unbounded read is never the price of
                # keep-alive.  The WebSocket upgrade leaves ``run()`` above and
                # never arrives here, which is why it refuses a bodied handshake
                # outright instead — see ``_do_ws_handshake``.
                if inner_receive.needs_drain():
                    if not await inner_receive.drain(_MAX_KEEPALIVE_DRAIN):
                        break

                # RFC 9112 §9.1 — honour Connection: close.  HTTP/1.0
                # connections without ``Connection: keep-alive`` likewise
                # default to non-persistent.
                if not self._should_keep_alive(conn):
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

    @staticmethod
    async def _send_error_and_close(send, body: bytes, status: HTTPStatus) -> None:
        """Send a plain-text error response with ``Connection: close``.

        Every framing/timeout guard in ``run()`` answers the same way: a
        short body, the status line, and ``connection: close`` +
        ``content-type: text/plain`` headers.
        """
        await send(
            body, status,
            [(b'connection', b'close'), (b'content-type', b'text/plain')],
        )

    def _parse(self, data: bytes) -> Connection:
        """Parse raw HTTP/1.1 request bytes into a native :class:`Connection`.

        The parser produces the typed native model; the ASGI scope is a
        derived view obtained via ``conn.as_scope()`` at the dispatch
        boundary.

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
        # Connection-scoped, so it is read once per connection rather than
        # once per request.  The cost being removed is not ``get_settings()``
        # itself (it is ``functools.cache``d) but the ``from ..env import``
        # statement that has to run ahead of it on every single parse.
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
        # RFC 9110 §2.5 / §15.6.6 — only major version 1 is served here
        # (RFC9112-2.3-INVALID-VERSION: ``HTTP/9.9`` was answered 200).
        # A higher 1.x minor (``HTTP/1.2``) is 1.x-compatible and served
        # as 1.1; any other major → 505.  The HTTP/2 preface never reaches
        # this parser — ``protocol_registry`` sniffs ``PRI * HTTP/2.0``
        # off the socket before the HTTP/1.1 binding is selected.
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
            # OPTIONS.  Flag it so the dispatcher answers at the server level
            # rather than routing ``*`` to a 404.
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
            # Split the bytes request-target with C-level
            # partition calls (~12× faster than urlparse): strip #fragment,
            # then split ?query.  ';' is NOT split off: RFC 3986
            # treats it as an ordinary path sub-delimiter (the ;params grammar
            # is obsolete RFC 2396), so it stays in the path component.  The
            # path component (raw_path) and its decoded form (path) are now
            # the same bytes at two decode stages — uvicorn parity.
            _no_frag, _, _ = path.partition(b'#')
            _raw_path_b, _, _query_string = _no_frag.partition(b'?')

        # ASGI: scope['path'] is the percent-decoded (UTF-8)
        # path component.  The b'%' guard keeps the no-escape common case on
        # the plain-decode fast path; the target was already rejected above
        # if it contains bytes >= 0x80, so .decode('ascii') cannot fail.
        # unquote semantics match uvicorn: '+' stays literal, malformed
        # escapes pass through, errors='replace' can never raise.
        if b'%' in _raw_path_b:
            _decoded_path = unquote(_raw_path_b.decode('ascii'),
                                  encoding='utf-8', errors='replace')
        else:
            _decoded_path = _raw_path_b.decode('utf-8')

        # One C-level pass decides whether any field value *could* hold a
        # forbidden octet.  When it says no, the per-header regex below is
        # dead weight and is skipped; when it says yes, that regex still runs
        # and still raises, so the diagnostic never changes.
        values_need_checking = not _block_values_are_clean(data)

        # Per-connection cache of lines that have already passed every check
        # below.  The key is the *exact* line bytes, so any changed byte is a
        # miss and gets validated from scratch; only a line that reached the
        # bottom of the loop without raising is ever admitted.  Both facts
        # together are what make this a replay of validated work rather than a
        # way to skip validation.  Created per instance on first use — see the
        # class-level ``None`` for why it is not a class attribute.
        cache = self._line_cache
        if cache is None:
            cache = self._line_cache = {}
        # Hashing a line to probe a cache that is known to be empty buys
        # nothing, and on the first request of a connection that is every
        # probe.  Skipping them is what keeps the connection-per-request shape
        # (``Connection: close``, HTTP/1.0, health checks) close to the cost of
        # not having a cache at all: that client pays for admissions it never
        # reads, but not for lookups that cannot hit.  Decided once per
        # request, not once per line.
        do_lookup = len(cache) > 0

        raw: list[tuple[bytes, bytes]] = []
        for line in lines[idx + 1:]:
            if not line:
                # Empty line = end of headers; anything after is body (already
                # split off upstream because we read until CRLFCRLF).
                continue
            # A line too long to ever be admitted skips the cache completely —
            # including the lookup, because hashing 8 KiB to answer a question
            # whose answer is always "no" is the adversary's cheapest way to
            # spend our CPU.  ``len`` on bytes is O(1).
            cacheable = len(line) <= _LINE_CACHE_MAX_LINE
            if cacheable:
                # The per-connection cache first — it holds this peer's long,
                # expensive lines — then the shared spec table, which is the
                # only thing that can help the *first* request on a connection.
                hit = cache.get(line) if do_lookup else None
                if hit is None:
                    hit = _DEFAULT_LINES.get(line)
                if hit is not None:
                    # Identical bytes, already validated — on this connection
                    # when it came from ``cache``, at import when it came from
                    # ``_DEFAULT_LINES``.  Safe even when
                    # ``values_need_checking`` is True for *this* block: the
                    # hit was proved clean when it was admitted and its bytes
                    # have not changed since.
                    raw.append(hit)
                    continue
            # RFC 9112 §5.2 — obs-fold (leading SP/HTAB on a header line)
            # MUST be rejected in requests.  Indexing yields an int and skips
            # the one-byte slice a `line[:1]` comparison would allocate; the
            # empty-line case is retired by the `continue` above.
            if line[0] in (0x20, 0x09):
                raise BadRequestError(
                    f'obsolete line folding rejected: {line!r}')
            colon = line.find(b':')
            if colon < 1:
                raise BadRequestError(f'malformed header line: {line!r}')
            key = line[:colon]
            value = line[colon + 1:]
            # field-name must be a valid token (§5.1 / RFC 9110 §5.6.2).
            # SP and HTAB are not tchar, so this one test also decides §5.1
            # (no whitespace between field-name and ':') — a well-formed name
            # answers both questions at once, and only a rejected name pays
            # for telling the two apart.  `colon < 1` above guarantees a
            # non-empty key, so indexing is safe.
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
            # Strict Content-Length — checked against the raw post-colon
            # bytes, before the OWS strip below erases the evidence.
            if lkey == b'content-length' and not _CL_STRICT_RE.match(value):
                raise BadRequestError(
                    f'ambiguous Content-Length value {value!r} '
                    f'(RFC 9110 §8.6)')
            # Strip the OWS surrounding the value (§5).
            value = value.strip(b' \t')
            # field-value MUST NOT contain CTLs except HTAB.
            if values_need_checking and _FIELD_VALUE_INVALID_RE.search(value):
                raise BadRequestError(
                    f'CTL in header value (smuggling / log-injection): '
                    f'{key!r}: {value!r}')
            pair = (lkey, value)
            raw.append(pair)
            # Admission is the last statement in the loop body on purpose:
            # every ``raise`` above is a line that never enters the cache.
            # Bytes are the bound that matters — see the constants above.
            # Tested against the *resulting* size, not the current one, so the
            # budget is a real ceiling rather than one a final line can step over.
            if (cacheable
                    and len(cache) < _LINE_CACHE_MAX
                    and self._line_cache_bytes + len(line) <= _LINE_CACHE_MAX_BYTES):
                cache[line] = pair
                self._line_cache_bytes += len(line)

        # RFC 9112 §3.2.2 — for an absolute-form target the request's own
        # authority is definitive; replace any client Host so a mismatched or
        # spoofed Host cannot influence routing / host validation
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

        # RFC 9112 §6 — message framing validation.  Done here so a bad
        # framing header is rejected before any body bytes are read.
        # Also validates Host (§3.2 / §7.2) and the
        # transfer-coding registry (§6.1 → 501 on unknown coding).
        _validate_message_framing(headers)
        _validate_host(headers)
        # RFC 9112 §3.2 / §7.2 — every HTTP/1.1 (and later 1.x) request
        # MUST carry a Host header (RFC9112-7.1-MISSING-HOST); only
        # HTTP/1.0, which predates Host, may omit it (COMP-HTTP10-NO-HOST).
        # ``_validate_host`` checks the value's grammar when present; the
        # presence rule is version-aware, so it lives here.
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
            # RFC-safe default.  ``root_path`` (mount prefix) is NOT taken
            # from the client-controlled ``X-Forwarded-Prefix`` here (bug
            # 1.16 — a client could spoof the mount point); only the
            # ``TrustedProxy`` middleware sets it, after verifying the peer.
            root_path='',
            headers=headers,
            client=None,
            server=None,
            extensions=_H1_PATHSEND_EXTENSIONS if not self._ssl else {},
        )

        if asterisk_form:
            # Server-wide OPTIONS (§3.2.4): mark it so the dispatcher answers
            # at the server level (200 with Allow) rather than routing ``*``.
            conn._asterisk_form = True

        if headers.getlist(b'host'):
            default_port = _HTTPS_PORT if self._ssl else _HTTP_PORT
            host, port = _parse_host_header(headers.get(b'host'), default_port)
            conn.server = (host, port)

        if headers.getlist(b'upgrade'):
            # RFC 9110 §7.8 — a server MAY ignore an Upgrade it does not
            # support; it MUST NOT fail the request over it.  Only WebSocket
            # switches the connection type.  Any other token (notably curl's
            # default ``Upgrade: h2c`` probe on ``--http2``) is ignored and
            # the request is served as ordinary HTTP/1.1 — previously any
            # unknown token became ``scope['type']`` and crashed dispatch,
            # closing the connection with no reply.
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

        log_record = _AccessLogRecord.from_conn(conn)
        log_record.status = 101  # HTTP 101 Switching Protocols

        if not await self._do_ws_handshake(conn):
            return  # version check failed; 400 already sent
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
            log_record.close_code = ws_actor._disconnect_code
            _emit_access_log(log_record)

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
        # Content on the handshake is refused before anything switches.  Those
        # octets are request content per ``Content-Length``/``Transfer-Encoding``
        # *and* the first frames per the 101 — two framings over the same bytes,
        # which is what a front end and this server would disagree about.  The
        # upgrade leaves ``run()`` before the keep-alive drain, so left alone
        # they are read back as frames and handed to the application as a
        # message.  Refusing beats draining here: RFC 9110 §9.3.1 gives content
        # on GET no defined semantics (unlike §9.3.7 for OPTIONS, where the
        # drain is the only correct answer), so nothing legitimate is lost, and
        # a refusal resolves the ambiguity instead of picking a side of it.
        # ``Content-Length: 0`` declares no content and so is not ambiguous —
        # clients and proxies do attach it to GET requests.  CL/TE conflicts and
        # malformed values are already 400/501 at parse, so a well-formed single
        # value is all that can reach here.
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

        # The client-offered subprotocols (ASGI websocket scope's
        # ``subprotocols``) are derived from the request header by the
        # ``conn.subprotocols`` property — no need to stash them.
        client_protos = conn.subprotocols

        # Auto-negotiate from app.available_ws_protocols (backward-compat fallback).
        # This is used when the handler calls websocket.accept without a subprotocol.
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

    @staticmethod
    def _make_legacy_disconnect_receive(receive, conn: dict | Connection, dispatcher, log_record):
        """Legacy disconnect-detecting receive wrapper (mirrors server.py helper)."""
        async def detecting_receive():
            event = await receive()
            if isinstance(event, dict) and event.get('type') == ASGIEvent.HTTP_DISCONNECT:
                if not disconnected(conn):
                    mark_disconnected(conn)
                    await dispatcher.emit(Event(
                        'request_disconnected',
                        detail={
                            'conn':        conn,
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
        conn: Connection,
        send,
        cfg,
        dl,
        inner_receive: 'HTTP1Recipient | None',
        loop_start_perf: float,
        loop_start_cpu: float,
    ) -> tuple[bool, 'HTTP1Recipient']:
        """Prepare and run one request; return ``(keep_alive, inner_receive)``.

        The per-request preparation lives here instead of in ``run`` so the
        keep-alive loop stays a skeleton and the hot path gains no new call
        site or coroutine boundary (AB-HIGH-PRECISION.md §6.1).
        """
        import asyncio  # noqa: PLC0415

        # Build the access-log record only when something consumes it
        # (access log / phase trace / request_completed listener). On the
        # baseline hot path — no logging, no listeners — skipping it drops
        # a per-request allocation and the ``conn.state`` dict it forces
        # (the Connection graph's per-request objects are what the
        # cyclic GC scans under concurrency). The
        # legacy (no-aggregator) branch of the dispatch reads the
        # record unconditionally, so keep building it when there is no
        # aggregator. Consumers below are all None-tolerant (the sender
        # guards ``_log_record is not None``; the finally guards ``emit``).
        if self._aggregator is None or _request_record_needed(self._aggregator):
            log_record = _AccessLogRecord.from_conn(conn)
            if _PHASE_TRACE:
                log_record.phases['loop_start'] = (
                    loop_start_perf, loop_start_cpu)
            log_record.mark('parsed')
            # Write onto ``conn.state`` directly (the same dict the scope
            # exposes as ``scope['state']``) so recording the access log
            # does not materialize the lazy scope.
            conn.state['access_log'] = log_record
        else:
            log_record = None
        # Reset per-request sender state.  The
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

        # RFC 9110 §10.1.1 / §15.2 — a server MUST NOT send a 1xx
        # response to an HTTP/1.0 client (COMP-NO-1XX-HTTP10); the
        # Expect header is ignored and the body read normally.
        #
        # Emitted *after* the reset: the sender is shared across keep-alive
        # requests, and its "response already complete" guard is still set
        # from the previous response until the reset clears it.  Written
        # before it, the interim response is silently dropped from request
        # two onward and the peer stalls until its own Expect timeout.
        # Before ``_log_record`` is attached, too, so the interim status
        # never lands in the record the real response owns.
        if (conn.http_version != '1.0'
                and conn.headers.get(b'expect').lower()
                == b'100-continue'):
            await send(b'', HTTPStatus.CONTINUE)

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
        # ``method`` is rewritten to ``GET`` on both the Connection
        # (which the router/dispatcher read) and the scope envelope
        # (which middleware may inspect); the access log records the
        # original ``HEAD`` from the request line.
        send._head_mode = (conn.method == 'HEAD')
        if send._head_mode:
            # Rewrite on the Connection only; a materialized scope reads
            # ``method`` back from ``conn`` (now 'GET'), so no separate
            # ``scope['method']`` write (which would force materialization).
            conn.method = 'GET'

        # The app's argument is built *here*, after every pre-dispatch
        # mutation of ``conn`` — the HEAD→GET rewrite above being the one
        # that matters.  The native lane passes the Connection itself and
        # so would tolerate an earlier binding, but the compat lane emits
        # a *snapshot*: taken any earlier it freezes ``method='HEAD'``,
        # the router finds no HEAD route, and the dual-path lane answers
        # 405 where the native lane answers 200 (COMP-HEAD-NO-BODY).
        if cfg.force_asgi_scope:
            app_arg = conn.to_asgi_scope(force_asgi=True)  # pure scope
        else:
            app_arg = conn                                     # native Connection
        # One recipient per connection, rebound per request — the same
        # trade as ``send.reset_per_request_state()`` above, and for the
        # same reason: the reader, body timeout, and deadline are
        # connection properties, so rebuilding them per request bought
        # nothing.  ``bind`` re-derives the framing from the new head.
        if inner_receive is None:
            inner_receive = RecipientFactory.http1(
                self._reader, conn,
                body_timeout=cfg.body_timeout,
                deadline=dl,
            )
        else:
            inner_receive.bind(conn)

        if conn._asterisk_form:
            # RFC 9112 §3.2.4 — ``OPTIONS *`` is a server-wide request
            # whose target is not a resource, so it never routes.
            # Answer at the server level with 204 + an Allow header
            # advertising the methods the origin implements.
            await capturing_send(b'', HTTPStatus.NO_CONTENT,
                                 [(b'allow', _SERVER_WIDE_ALLOW)])
            return True, inner_receive

        # Bind the *raw* recipient onto the app argument for lazy
        # ``app_arg.body()`` before any disconnect-detecting wrapper is
        # built. Binding the wrapper instead would close a per-request
        # reference cycle (app_arg._receive → wrapper → app_arg) reclaimable
        # only by the cyclic GC — the v0.60.0 tail-latency regression.
        # Idempotent (only binds when unset).
        bind_receive_channel(app_arg, inner_receive)
        if self._aggregator is not None:
            # Wrap receive for disconnect detection only when a listener observes
            # it (request_disconnected, or request_completed via mark_disconnected);
            # otherwise dispatch the raw receive and save the per-request closure.
            if _disconnect_events_observed(self._aggregator):
                detecting_receive = _make_disconnect_detecting_receive(
                    inner_receive, app_arg, self._aggregator)
            else:
                detecting_receive = inner_receive
            request_actor = self._request_actor
            if request_actor is None:
                request_actor = self._request_actor = RequestActor(
                    app_arg, detecting_receive, capturing_send,
                    self._app, self._aggregator,
                )
            else:
                request_actor.bind(app_arg, detecting_receive, capturing_send)
            try:
                await request_actor.run()
            except asyncio.CancelledError:
                # Let BB_REQUEST_TIMEOUT's wait_for see
                # the cancellation; swallowing it here would convert a
                # timeout into a normal close without the 408 synthesis.
                raise
            except Exception:
                return False, inner_receive
            finally:
                if log_record is not None:
                    log_record.mark('dispatch_done')
                    _emit_access_log(log_record)
        else:
            _dispatcher = getattr(self._app, '_dispatcher', None)
            if _dispatcher is not None:
                detecting_receive = self._make_legacy_disconnect_receive(
                    inner_receive, app_arg, _dispatcher, log_record)
            else:
                detecting_receive = inner_receive
            try:
                await self._app(app_arg, detecting_receive, capturing_send)
            except BaseException:
                raise
            finally:
                log_record.mark('dispatch_done')
                _emit_access_log(log_record)
        return True, inner_receive

    async def _read_headers(self, max_total: int) -> None:
        """Drain bytes from the reader until ``\\r\\n\\r\\n`` is at the end of
        ``self._request``.  Enforces the configured total-block size limit;
        raises :class:`HeaderTooLargeError` when the buffer overshoots.

        Read **one CRLF-terminated line per iteration** rather than scanning
        for the contiguous ``\\r\\n\\r\\n`` delimiter.  A
        ``readuntil(b'\\r\\n\\r\\n')`` shape deadlocks when
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
        read_head = getattr(self._reader, 'read_head', None)
        # The scan applies only when nothing has been consumed on this
        # message's behalf.  A non-empty ``_request`` means bytes were taken
        # off the stream upstream (protocol detection's replayed prefix, or a
        # complete head handed in) — scanning then would consume the *next*
        # message's head instead of finding this one's.  The buffer-owning
        # front end never pre-consumes, so it always takes this path.
        if read_head is not None and not self._request:
            # Dispatch on the reader's capability rather than a setting: which
            # reader a connection has is decided at accept.  The line-by-line
            # loop below lives only as long as connections still arrive
            # through ``asyncio.StreamReader``.
            try:
                head = await read_head(max_total)
            except HeadTooLargeError as exc:
                # Same rule as the line loop, one implementation — see
                # ``_reject_oversized_head``.  Testing for "no CRLF at all"
                # would not do: the whole request usually arrives in one
                # burst, terminator included, so the question is whether the
                # *first* CRLF is inside the budget.
                _reject_oversized_head(
                    self._reader.peek(min(self._reader.buffered_len(),
                                          max_total + 2)),
                    max_total)
            if not head:
                # EOF without a complete head.  ``run()`` tells an idle close
                # from a truncated one by whether bytes arrived, so surface
                # what did rather than an empty request.
                self._request = self._reader.peek(self._reader.buffered_len())
                raise IncompleteReadError(self._request)
            self._request = head
            return
        # Accumulate into a bytearray (amortised O(1) append) instead of the
        # O(n²) bytes ``+=`` growth, then publish back as bytes.  The loop
        # condition and size check are byte-for-byte equivalent to the prior
        # form.
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
                _reject_oversized_head(bytes(buf), max_total)
        self._request = bytes(buf)

    def _should_keep_alive(self, conn) -> bool:
        """Return True if the connection should persist after this request.

        Reads the :class:`Connection` directly so the
        per-request keep-alive decision never materializes the lazy scope."""
        http_version = conn.http_version
        connection = conn.headers.get(b'connection', b'').lower()
        if http_version == '1.1':
            return connection != b'close'
        return connection == b'keep-alive'

    async def _handle(self, msg: Message) -> None:  # never reached
        raise NotImplementedError
