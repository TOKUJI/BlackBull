from urllib.parse import unquote, urlsplit

from ..protocol.frame_types import PseudoHeaders
import logging
from ..connection import Connection
from ..headers import Headers
from .http1_actor import _HOST_FORBIDDEN_BYTES

logger = logging.getLogger(__name__)


def _default_connection() -> Connection:
    """Minimal HTTP/2 :class:`Connection` returned on the malformed / early-out
    paths of :func:`parse_headers`.

    The actor checks ``frame.malformed`` and answers RST_STREAM before it ever
    reads this object, so only the defaults matter — it exists to keep the
    return contract a :class:`Connection` on every path (Sprint 79 Phase 4,
    the H/2 analogue of :meth:`HTTP1Actor._parse` always yielding a Connection).
    """
    return Connection(
        method='HEAD', path='', raw_path=b'', headers=Headers([]),
        http_version='2', scheme='https',
    )


def _split_h2_path(raw: str):
    """Split an HTTP/2 ``:path`` pseudo into ASGI (path, raw_path, query_string).

    RFC 9113 §8.3.1: ``:path`` carries the origin-form request target
    (path + optional query) joined by ``?``.  ASGI requires
    ``scope['path']`` to be the percent-decoded (UTF-8) path component
    (str), ``scope['raw_path']`` the undecoded path-component bytes, and
    ``scope['query_string']`` the raw query as ``bytes``.

    ``raw`` is always ``str`` — pseudo-header values are normalised to
    ``str`` when the HEADERS frame is parsed (``frame_types`` decodes them),
    and the server-push caller passes the ASGI event's ``str`` path.

    Sprint 68 — ``urlsplit`` (not ``urlparse``) so an RFC 3986 ``;`` path
    sub-delimiter is kept in the path component rather than split off as
    obsolete RFC 2396 ``;params`` (``urlparse`` would strip it from both
    ``path`` and ``raw_path``).  The ``'%' in path`` guard keeps escape-free
    targets on the plain fast path; unquote semantics match uvicorn ('+'
    stays literal, malformed escapes pass through, ``errors='replace'`` can
    never raise).
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
    a present ``:authority`` replaces any literal ``Host`` in the header
    list handed to the application, mirroring H1's absolute-form
    override (RFC 9112 §3.2.2) so handlers see one ``host`` under
    either transport (ASGI host mapping).

    Returns the header list for ``Headers(...)``, or ``None`` after
    marking the frame malformed (the actor then answers RST_STREAM
    PROTOCOL_ERROR, the §8.3.1 stream error).
    """
    authority = frame.pseudo_headers.get(PseudoHeaders.AUTHORITY)
    if authority is not None:
        value = authority.encode('utf-8')
        if not value:
            frame._mark_malformed('empty :authority')
            return None
        if any(b in _HOST_FORBIDDEN_BYTES for b in value):
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
    if any(b in _HOST_FORBIDDEN_BYTES for b in value):
        frame._mark_malformed(
            f'invalid Host authority {value!r}: contains delimiter / '
            f'whitespace forbidden by RFC 3986 §3.2')
        return None
    return frame.headers


def parse_headers(frame) -> Connection:
    """Build a native :class:`Connection` (``http`` or ``websocket``) from a
    HEADERS frame.

    Hot path on every request — kept as a module-level function so that
    callers avoid the dict-lookup + parser allocation that ``ParserFactory``
    requires.

    Sprint 79 Phase 4: yields a :class:`Connection` rather than an ASGI scope
    dict (the H/2 analogue of :meth:`HTTP1Actor._parse`).  The actor derives
    the compat scope via :meth:`Connection.as_scope` at the dispatch boundary
    until Phase 5 switches the consumers onto ``conn`` directly; the
    websocket-only ``subprotocols`` scope key is likewise attached there (it is
    not a :class:`Connection` field — see the proposal §2.1 field set), the
    same way :meth:`HTTP1Actor._handle_upgrade` augments the derived scope.

    Also performs request-level pseudo-header presence checks (RFC 9113
    §8.3.1).  Field-level checks already happened in ``parse_payload``; if
    that flagged ``frame.malformed`` we still return a Connection to keep the
    contract simple but the actor will discard it before dispatch.  If
    parse_headers itself finds a missing or empty required pseudo, it sets
    ``frame.malformed`` so the same actor check rejects the request.
    """
    # Short-circuit if the frame parser already flagged this malformed.
    if getattr(frame, 'malformed', False):
        return _default_connection()

    # RFC 9113 §8.3.1 — ":status" is a response pseudo-header and MUST NOT
    # appear in a request.  ``parse_payload`` accepted it as a known pseudo-
    # header; we reject it here at the request layer.
    if PseudoHeaders.STATUS in frame.pseudo_headers:
        frame._mark_malformed('response pseudo-header in request: :status')
        return _default_connection()

    # RFC 9113 §8.3.1 — required request pseudo-headers.
    # CONNECT (RFC 9113 §8.5) omits :scheme and :path; the WebSocket
    # extension (RFC 8441) is detected below.
    method = frame.pseudo_headers.get(PseudoHeaders.METHOD)
    if method is None:
        frame._mark_malformed('missing :method')
        return _default_connection()
    if method != 'CONNECT':
        if PseudoHeaders.SCHEME not in frame.pseudo_headers:
            frame._mark_malformed('missing :scheme')
            return _default_connection()
        path = frame.pseudo_headers.get(PseudoHeaders.PATH)
        if path is None:
            frame._mark_malformed('missing :path')
            return _default_connection()
        if path == '':
            frame._mark_malformed('empty :path')
            return _default_connection()

    conn = _default_connection()

    protocol = frame.pseudo_headers.get(PseudoHeaders.PROTOCOL, '')

    if method == 'CONNECT' and protocol == 'websocket':
        # RFC 8441 §4 — Extended CONNECT bootstrapping WebSocket over HTTP/2
        conn.type = 'websocket'
        scheme = frame.pseudo_headers.get(PseudoHeaders.SCHEME, 'https')
        conn.scheme = 'wss' if scheme == 'https' else 'ws'
        if path := frame.pseudo_headers.get(PseudoHeaders.PATH):
            conn.path, conn.raw_path, conn.query_string = \
                _split_h2_path(path)
        # RFC 8441 requests carry ``:authority`` too — same grammar check
        # and ``host`` mapping as plain requests, but presence is not
        # enforced (the Extended CONNECT handshake already succeeded).
        raw_headers = _request_headers_with_host(frame, require_present=False)
        if raw_headers is None:
            return conn
        conn.headers = Headers(raw_headers)
        # Bug 1.16 — root_path is NOT taken from the client-controlled
        # X-Forwarded-Prefix; only TrustedProxy sets it after verifying the
        # peer.  Default to the RFC-safe empty mount.  ``subprotocols`` is
        # derived from the request headers by the actor bridge.
        conn.root_path = ''
        return conn

    if method:
        conn.method = method

    if path := frame.pseudo_headers.get(PseudoHeaders.PATH):
        conn.path, conn.raw_path, conn.query_string = \
            _split_h2_path(path)

    if scheme := frame.pseudo_headers.get(PseudoHeaders.SCHEME):
        conn.scheme = scheme

    # RFC 9113 §8.3.1 — validate the host authority and surface
    # ``:authority`` as the ``host`` header (ASGI).  Plain CONNECT is
    # excluded: §8.5 gives its ``:authority`` tunnel semantics, and the
    # presence rule only binds http/https requests.
    if method == 'CONNECT':
        conn.headers = Headers(frame.headers)
    else:
        raw_headers = _request_headers_with_host(
            frame, require_present=conn.scheme in ('http', 'https'))
        if raw_headers is None:
            return conn
        conn.headers = Headers(raw_headers)

    # Bug 1.16 — root_path is NOT taken from the client-controlled
    # X-Forwarded-Prefix; only TrustedProxy sets it after verifying the peer.
    conn.root_path = ''

    return conn
