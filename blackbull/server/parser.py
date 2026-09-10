from urllib.parse import unquote, urlsplit

from ..protocol.frame_types import PseudoHeaders
import logging
from ..connection import Connection
from ..headers import Headers
from .http1_actor import _HOST_FORBIDDEN_RE

logger = logging.getLogger(__name__)

# Shared empty extensions dict for the plain-HTTP/2 dispatch path — safe only
# because ``HTTP2Actor._apply_priority_and_extensions`` replaces
# ``conn.extensions`` with a fresh per-stream dict before the app or any
# middleware sees the Connection, so user code never reads or mutates this one.
# Same convention as ``http1_actor._H1_PATHSEND_EXTENSIONS``.
#
# The RFC 8441 WebSocket branch does NOT go through that call, so it must keep
# a dict of its own.
_EMPTY_H2_EXTENSIONS: dict = {}


def _build_h2_connection(method: str, path: str, raw_path: bytes,
                         query_string: bytes, headers: Headers,
                         scheme: str) -> Connection:
    """Lean constructor for the plain-HTTP/2 :func:`parse_headers` return.

    Bypasses the dataclass-generated ``Connection.__init__`` (type-call +
    default-binding machinery, ~200 ns/req) via ``object.__new__`` + explicit
    slot stores.  ``tests/architecture/test_h2_connection_builder.py`` pins it
    field-for-field against the dataclass and must be kept in sync with any
    change to :class:`Connection`'s field set.

    The RFC 8441 WebSocket branch is cold — one Extended CONNECT per session,
    not one per request — and keeps the plain constructor.
    """
    c = object.__new__(Connection)
    c.method = method
    c.path = path
    c.raw_path = raw_path
    c.headers = headers
    c.query_string = query_string
    c.http_version = '2'
    c.scheme = scheme
    c.client = None
    c.server = None
    c.state = {}
    c._path_params = None
    c.root_path = ''
    c.type = 'http'
    c.extensions = _EMPTY_H2_EXTENSIONS
    c.connection_id = ''
    c._asterisk_form = False
    c._body = None
    c._body_read = False
    c._cookies = None
    c._query = None
    c._query_list = None
    c._form = None
    c._receive = None
    c._disconnected = False
    c._ws = None
    return c


def _split_h2_path(raw: str):
    """Split an HTTP/2 ``:path`` pseudo into ASGI (path, raw_path, query_string).

    RFC 9113 §8.3.1: ``:path`` carries the origin-form request target
    (path + optional query) joined by ``?``.

    ``raw`` is always ``str`` — ``frame_types`` decodes pseudo-header values
    when the HEADERS frame is parsed, and the server-push caller passes the
    ASGI event's ``str`` path.

    ``urlsplit`` (not ``urlparse``) so an RFC 3986 ``;`` path sub-delimiter
    stays in the path component rather than being split off as obsolete RFC
    2396 ``;params``.  The ``'%' in path`` guard keeps escape-free targets on
    the plain fast path; unquote semantics match uvicorn ('+' stays literal,
    malformed escapes pass through, ``errors='replace'`` can never raise).
    """
    parsed = urlsplit(raw)
    path = parsed.path
    if '%' in path:
        decoded = unquote(path, encoding='utf-8', errors='replace')
    else:
        decoded = path
    return decoded, path.encode('utf-8'), parsed.query.encode('utf-8')


def _request_headers_with_host(frame, *, require_present: bool) -> list | None:
    """Validate the request's host authority and map ``:authority`` → ``host``.

    RFC 9113 §8.3.1 — ``:authority`` MUST NOT include userinfo; an
    ``http``/``https`` request without ``:authority`` must carry a valid
    ``Host`` field (*require_present*).  The grammar is H1's
    ``_validate_host`` (RFC 3986 §3.2 delimiters, same forbidden set);
    a present ``:authority`` replaces any literal ``Host`` handed to the
    application, mirroring H1's absolute-form override (RFC 9112 §3.2.2)
    so handlers see one ``host`` under either transport.

    Returns the header list for ``Headers(...)``, or ``None`` after
    marking the frame malformed (the actor then answers RST_STREAM
    PROTOCOL_ERROR).
    """
    authority = frame.pseudo_headers.get(PseudoHeaders.AUTHORITY)
    if authority is not None:
        value = authority.encode('utf-8')
        if not value:
            frame._mark_malformed('empty :authority')
            return None
        if _HOST_FORBIDDEN_RE.search(value):
            frame._mark_malformed(
                f'invalid :authority {authority!r}: contains userinfo, '
                f'delimiter, or whitespace forbidden by RFC 3986 §3.2')
            return None
        return ([(k, v) for (k, v) in frame.headers if k != b'host']
                + [(b'host', value)])

    hosts = [v for (k, v) in frame.headers if k == b'host']
    if len(hosts) > 1:
        frame._mark_malformed(
            f'multiple Host headers ({len(hosts)} — smuggling vector)')
        return None
    if not hosts:
        if require_present:
            frame._mark_malformed('missing :authority and Host')
            return None
        return frame.headers
    value = hosts[0].strip(b' \t')
    if not value:
        frame._mark_malformed('empty Host header value')
        return None
    if _HOST_FORBIDDEN_RE.search(value):
        frame._mark_malformed(
            f'invalid Host authority {value!r}: contains delimiter / '
            f'whitespace forbidden by RFC 3986 §3.2')
        return None
    return frame.headers


def parse_headers(frame) -> Connection | None:
    """Build a native :class:`Connection` (``http`` or ``websocket``) from a
    HEADERS frame, or ``None`` when the request is malformed.

    ``result is None`` if and only if ``frame.malformed``.  Every early-out
    returns ``None`` rather than a half-built :class:`Connection`, so a caller
    checks ``frame.malformed`` and never reads a partial object, and nothing
    is constructed on the error path.

    Also performs request-level pseudo-header presence checks (RFC 9113
    §8.3.1); field-level checks already happened in ``parse_payload``.

    A module-level function rather than a ``ParserFactory`` product: this runs
    on every request, and the factory's dict lookup and parser allocation
    would be paid per request for nothing.  The Internals page states why the
    read path threads a :class:`Connection` rather than an ASGI scope dict.
    """
    # Short-circuit if the frame parser already flagged this malformed.
    if getattr(frame, 'malformed', False):
        return None

    # RFC 9113 §8.3.1 — ":status" is a response pseudo-header and MUST NOT
    # appear in a request.  ``parse_payload`` accepted it as a known pseudo-
    # header; we reject it here at the request layer.
    if PseudoHeaders.STATUS in frame.pseudo_headers:
        frame._mark_malformed('response pseudo-header in request: :status')
        return None

    # RFC 9113 §8.3.1 — required request pseudo-headers.
    # CONNECT (RFC 9113 §8.5) omits :scheme and :path; the WebSocket
    # extension (RFC 8441) is detected below.
    method = frame.pseudo_headers.get(PseudoHeaders.METHOD)
    if method is None:
        frame._mark_malformed('missing :method')
        return None
    if method != 'CONNECT':
        if PseudoHeaders.SCHEME not in frame.pseudo_headers:
            frame._mark_malformed('missing :scheme')
            return None
        path_pseudo = frame.pseudo_headers.get(PseudoHeaders.PATH)
        if path_pseudo is None:
            frame._mark_malformed('missing :path')
            return None
        if path_pseudo == '':
            frame._mark_malformed('empty :path')
            return None

    protocol = frame.pseudo_headers.get(PseudoHeaders.PROTOCOL, '')

    if method == 'CONNECT' and protocol == 'websocket':
        # RFC 8441 §4 — Extended CONNECT bootstrapping WebSocket over HTTP/2.
        # ``method`` is the true wire value, never a placeholder: it IS read
        # for websocket-typed Connections, by ``AccessLogRecord.from_conn``
        # and by any global middleware whose method gate lacks a ``conn.type``
        # guard (``Cache``'s cacheable-methods check, for one).  Routing and
        # lifecycle events are unaffected — ``BlackBull._dispatch`` branches on
        # ``conn.type`` before any method-based dispatch.
        scheme_pseudo = frame.pseudo_headers.get(PseudoHeaders.SCHEME, 'https')
        path, raw_path, query_string = '', b'', b''
        if p := frame.pseudo_headers.get(PseudoHeaders.PATH):
            path, raw_path, query_string = _split_h2_path(p)
        # RFC 8441 requests carry ``:authority`` too — same grammar check
        # and ``host`` mapping as plain requests, but presence is not
        # enforced (the Extended CONNECT handshake already succeeded).
        raw_headers = _request_headers_with_host(frame, require_present=False)
        if raw_headers is None:
            return None
        # ``subprotocols`` is derived from the request headers by the actor
        # bridge, not stored here.
        return Connection(
            type='websocket', method=method,
            scheme='wss' if scheme_pseudo == 'https' else 'ws',
            path=path, raw_path=raw_path, query_string=query_string,
            headers=Headers.from_lowered(raw_headers), http_version='2',
        )

    path, raw_path, query_string = '', b'', b''
    if p := frame.pseudo_headers.get(PseudoHeaders.PATH):
        path, raw_path, query_string = _split_h2_path(p)

    # An absent or empty ``:scheme`` falls back to 'https'.  Absence is
    # already rejected above for non-CONNECT requests, so this only covers
    # CONNECT and the empty-value case.
    scheme = frame.pseudo_headers.get(PseudoHeaders.SCHEME) or 'https'

    # Plain CONNECT is excluded from the host check: §8.5 gives its
    # ``:authority`` tunnel semantics, and the presence rule only binds
    # http/https requests.
    #
    # ``from_lowered`` is safe on every H/2 path: §8.2.1 makes an uppercase
    # field name malformed and ``HeadersFrame.parse_payload`` rejects the
    # frame before the pair reaches ``frame.headers``, so the list is
    # lowercase by protocol.  The injected ``host`` is a lowercase literal.
    if method == 'CONNECT':
        headers = Headers.from_lowered(frame.headers)
    else:
        raw_headers = _request_headers_with_host(
            frame, require_present=scheme in ('http', 'https'))
        if raw_headers is None:
            return None
        headers = Headers.from_lowered(raw_headers)

    # A spec-illegal empty ``:method`` falls back to 'HEAD' instead of being
    # rejected the way an empty ``:path`` is.  That asymmetry is a known
    # conformance gap, deliberately left alone — closing it is a behaviour
    # change, not a cleanup.
    effective_method = method or 'HEAD'

    # ``root_path`` is NOT taken from the client-controlled X-Forwarded-Prefix;
    # only TrustedProxy sets it after verifying the peer.  Both branches leave
    # it at the field default ('') — the RFC-safe empty mount.
    return _build_h2_connection(effective_method, path, raw_path,
                                query_string, headers, scheme)
