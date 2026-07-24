from urllib.parse import unquote, urlsplit

from ..protocol.frame_types import PseudoHeaders
import logging
from ..connection import Connection
from ..headers import Headers
from .http1_actor import _HOST_FORBIDDEN_BYTES

logger = logging.getLogger(__name__)

# Sprint 80 alloc hygiene (`proposals/connection-alloc-hygiene.md` Phase 2,
# 2026-07-24) — a shared empty extensions dict for the plain-HTTP/2 dispatch
# path. ``HTTP2Actor._apply_priority_and_extensions`` unconditionally replaces
# ``conn.extensions`` with a fresh per-stream dict *before* the app or any
# middleware sees the Connection (``_on_headers_frame`` / ``_on_continuation_
# frame``, both call it ahead of ``_spawn_stream_task``), so this sentinel is
# never read or mutated by user code — same convention as
# ``http1_actor._H1_PATHSEND_EXTENSIONS``. The RFC 8441 WebSocket branch of
# ``parse_headers`` does NOT go through ``_apply_priority_and_extensions``, so
# it must NOT use this sentinel — it keeps a fresh ``{}`` (the dataclass
# default) for the connection's lifetime.
_EMPTY_H2_EXTENSIONS: dict = {}


def _build_h2_connection(method: str, path: str, raw_path: bytes,
                         query_string: bytes, headers: Headers,
                         scheme: str) -> Connection:
    """Lean constructor for the plain-HTTP/2 :func:`parse_headers` return.

    Sprint 80 alloc hygiene Phase 2: bypasses the dataclass-generated
    ``Connection.__init__`` (type-call + default-binding machinery, ~200 ns/req
    measured in `bench/results/h2-alloc-cpu-ab/20260723-161201Z-conn-decomp/`)
    via ``object.__new__`` + explicit slot stores. Behaviourally identical to
    ``Connection(method=method, path=path, raw_path=raw_path,
    query_string=query_string, headers=headers, http_version='2',
    scheme=scheme)`` — pinned field-for-field by
    ``tests/architecture/test_h2_connection_builder.py``, which must be kept
    in sync with any change to :class:`Connection`'s field set.

    Only used by the plain-HTTP branch of ``parse_headers`` — the RFC 8441
    WebSocket branch is a cold path (one Extended CONNECT per WS-over-H2
    session, not one per request) and keeps the plain dataclass constructor.
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
    c._receive = None
    c._disconnected = False
    c._ws = None
    return c


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


def parse_headers(frame) -> Connection | None:
    """Build a native :class:`Connection` (``http`` or ``websocket``) from a
    HEADERS frame, or ``None`` when the request is malformed.

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

    Sprint 80 alloc hygiene (`proposals/connection-alloc-hygiene.md`,
    2026-07-24): builds the :class:`Connection` **once**, at the very end, from
    locals accumulated while walking the pseudo-headers — the same
    single-construction idiom :meth:`HTTP1Actor._parse` already uses (Phase 1).
    The previous version built a placeholder via a removed
    ``_default_connection`` helper (with a throwaway ``Headers([])``, discarded
    ~154 ns/req) and mutated it field by field. Every early-out below now
    returns ``None`` instead of a half-built ``Connection`` — including the
    (previously reachable but never-observed) host-validation-failure path,
    since ``_request_headers_with_host`` already marks ``frame.malformed``
    before returning ``None``, and every caller checks ``frame.malformed``
    before reading this function's result. The contract is now uniformly
    ``result is None ⟺ frame.malformed``. The plain-HTTP branch's final
    construction goes through ``_build_h2_connection`` (Phase 2 — an
    ``object.__new__`` lean builder), not the dataclass constructor; see that
    function's docstring.

    Also performs request-level pseudo-header presence checks (RFC 9113
    §8.3.1).  Field-level checks already happened in ``parse_payload``.  On any
    known-bad input — ``parse_payload`` having flagged ``frame.malformed``, or a
    missing/empty required pseudo found here (which sets ``frame.malformed``) —
    we return ``None`` rather than build a throwaway :class:`Connection`: the
    actor's ``frame.malformed`` check answers RST_STREAM before it would read
    the result, so no object is constructed on the error path.
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
        # ``method='CONNECT'`` here (the true wire value) — the previous
        # per-field-mutation version left this at the ``_default_connection``
        # placeholder ('HEAD') because the old code path only assigned
        # ``conn.method`` outside this branch. This is an intentional,
        # observable correction: ``conn.method`` IS read for websocket-typed
        # Connections — by ``AccessLogRecord.from_conn`` (the WS-over-H2
        # access-log line now records CONNECT, matching H/1.1 upgrades which
        # record their true GET) and by any installed global middleware whose
        # method gate lacks a ``conn.type`` guard (e.g. ``Cache``'s
        # cacheable-methods check, which the 'HEAD' placeholder wrongly
        # satisfied for WS-over-H2 requests). Routing and lifecycle events
        # are unaffected — ``BlackBull._dispatch`` branches on ``conn.type``
        # before any method-based dispatch.
        scheme_pseudo = frame.pseudo_headers.get(PseudoHeaders.SCHEME, 'https')
        path, raw_path, query_string = '', b'', b''
        if p := frame.pseudo_headers.get(PseudoHeaders.PATH):
            path, raw_path, query_string = _split_h2_path(p)
        # RFC 8441 requests carry ``:authority`` too — same grammar check
        # and ``host`` mapping as plain requests, but presence is not
        # enforced (the Extended CONNECT handshake already succeeded).
        raw_headers = _request_headers_with_host(frame, require_present=False)
        if raw_headers is None:
            return None  # frame already marked malformed by the helper
        # Bug 1.16 — root_path is NOT taken from the client-controlled
        # X-Forwarded-Prefix; only TrustedProxy sets it after verifying the
        # peer.  Left at the field default ('') — the RFC-safe empty mount.
        # ``subprotocols`` is derived from the request headers by the actor
        # bridge, not stored here.
        return Connection(
            type='websocket', method=method,
            scheme='wss' if scheme_pseudo == 'https' else 'ws',
            path=path, raw_path=raw_path, query_string=query_string,
            headers=Headers(raw_headers), http_version='2',
        )

    path, raw_path, query_string = '', b'', b''
    if p := frame.pseudo_headers.get(PseudoHeaders.PATH):
        path, raw_path, query_string = _split_h2_path(p)

    # The previous version's ``if scheme := …: conn.scheme = scheme`` left the
    # ``_default_connection`` placeholder ('https') in place when ``:scheme``
    # was absent or empty; ``or 'https'`` reproduces that exactly.
    scheme = frame.pseudo_headers.get(PseudoHeaders.SCHEME) or 'https'

    # RFC 9113 §8.3.1 — validate the host authority and surface
    # ``:authority`` as the ``host`` header (ASGI).  Plain CONNECT is
    # excluded: §8.5 gives its ``:authority`` tunnel semantics, and the
    # presence rule only binds http/https requests.
    if method == 'CONNECT':
        headers = Headers(frame.headers)
    else:
        raw_headers = _request_headers_with_host(
            frame, require_present=scheme in ('http', 'https'))
        if raw_headers is None:
            return None  # frame already marked malformed by the helper
        headers = Headers(raw_headers)

    # The previous version's ``if method: conn.method = method`` silently kept
    # the 'HEAD' placeholder for a (spec-illegal, never observed in practice)
    # empty ``:method`` value; ``or 'HEAD'`` reproduces that pre-existing
    # quirk exactly rather than silently changing it as a refactor side
    # effect. Fixing this latent gap (empty ``:method`` isn't rejected the
    # way empty ``:path`` is, above) is a conformance question, out of scope
    # here.
    effective_method = method or 'HEAD'

    # Bug 1.16 — root_path is NOT taken from the client-controlled
    # X-Forwarded-Prefix; only TrustedProxy sets it after verifying the peer.
    # Left at the field default ('').
    return _build_h2_connection(effective_method, path, raw_path,
                                query_string, headers, scheme)
