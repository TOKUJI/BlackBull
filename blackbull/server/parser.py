"""HTTP request parsing into native Connection objects.

Keep framing and field validation in shared grammar helpers. Fast paths may
reuse validated bytes but cannot weaken checks or change error scope.
"""
from ..protocol.frame_types import PseudoHeaders
from ..protocol.field_grammar import (COMMON_METHODS, COMMON_SCHEMES,
                                      URI_SCHEME_RE, method_token_is_valid)
from ..protocol.framing import method_is
import logging
from ..connection import Connection
from ..headers import Headers
from ..protocol.field_grammar import FieldError, authority_is_valid, host_field_value
from .http1_actor import _TARGET_ALLOWED_OCTETS
from .request_target import split_path_query

logger = logging.getLogger(__name__)

# Plain HTTP shares this empty dictionary only until _apply_priority_and_extensions
# replaces it before dispatch. WebSocket branches need their own dictionary.
_EMPTY_H2_EXTENSIONS: dict = {}


def _build_h2_connection(method: str, path: str, raw_path: bytes,
                         query_string: bytes, headers: Headers,
                         scheme: str) -> Connection:
    """Build the native HTTP/2 Connection without the dataclass initializer.

    Keep every field synchronized with Connection. The parity gate is
    tests/architecture/test_h2_connection_builder.py.
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


def _split_h2_path(raw: str) -> tuple[str, bytes, bytes]:
    """Split an HTTP/2 ``:path`` pseudo into ASGI (path, raw_path, query_string).

    RFC 9113 §8.3.1: ``:path`` carries the origin-form request target
    (path + optional query) joined by ``?``.

    ``raw`` is always ``str`` — ``frame_types`` decodes pseudo-header values
    when the HEADERS frame is parsed, and the server-push caller passes the
    ASGI event's ``str`` path.
    """
    return split_path_query(raw.encode('utf-8'))


def _request_headers_with_host(frame, *, require_present: bool) -> list | None:
    """Return the request's fields with at most one ``host``: ``:authority``
    when present (replacing any Host), else the one Host field; either is a
    valid authority.

    *require_present* demands one of them (RFC 9113 §8.3.1).  Returns ``None``
    after marking the frame malformed (RST_STREAM PROTOCOL_ERROR).
    """
    authority = frame.pseudo_headers.get(PseudoHeaders.AUTHORITY)
    if authority is not None:
        value = authority.encode('utf-8')
        if not value:
            frame._mark_malformed('empty :authority')
            return None
        if not authority_is_valid(value):
            frame._mark_malformed(f'invalid :authority {authority!r}')
            return None
        return ([(k, v) for (k, v) in frame.headers if k != b'host']
                + [(b'host', value)])

    try:
        host = host_field_value([f for f in frame.headers if f[0] == b'host'])
    except FieldError as exc:
        frame._mark_malformed(str(exc))
        return None
    if host is None and require_present:
        frame._mark_malformed('missing :authority and Host')
        return None
    return frame.headers


def parse_headers(frame) -> Connection | None:
    """Build a native HTTP or WebSocket Connection, or None for a malformed head.

    The Connection's header names are lowercase tchar, its values carry no edge
    SP/HTAB and no CTL, ``host`` is one valid authority taken from
    ``:authority`` or Host, and ``scheme`` is lowercase.
    None must coincide with frame.malformed; never expose partial connections.
    Validate required pseudo-fields and request-target octets (RFC 9113 §8.3.1)
    after field-level checks in parse_payload.
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
    # RFC 9110 §9.1 — method = token.  HTTP/1.1 grades its request line with
    # the same rule (``http1_actor._parse``), so the two transports cannot
    # disagree about which methods exist.
    if (method not in COMMON_METHODS
            and not method_token_is_valid(method.encode('utf-8'))):
        frame._mark_malformed(f'invalid :method {method!r}')
        return None

    # RFC 9112 §2.1 with RFC 3986 — the visible-ASCII rule HTTP/1.1 applies to
    # its request-target, so the transports cannot disagree about a path
    # (MAL-NON-ASCII-URL).  Graded whenever the field is present: the CONNECT
    # forms below read ``:path`` too.
    path_pseudo = frame.pseudo_headers.get(PseudoHeaders.PATH)
    if (path_pseudo is not None
            and path_pseudo.encode('utf-8').translate(
                None, _TARGET_ALLOWED_OCTETS)):
        frame._mark_malformed(f'invalid :path {path_pseudo!r}')
        return None

    # RFC 3986 §3.1: grade a present scheme before using it for authority
    # or transport mapping.
    scheme_pseudo = frame.pseudo_headers.get(PseudoHeaders.SCHEME)
    if scheme_pseudo is None:
        scheme = 'https'                # CONNECT omits :scheme (RFC 9113 §8.5)
    elif scheme_pseudo in COMMON_SCHEMES:
        scheme = scheme_pseudo          # members are lowercase — test-pinned
    elif URI_SCHEME_RE.fullmatch(scheme_pseudo.encode('utf-8')) is None:
        frame._mark_malformed(f'invalid :scheme {scheme_pseudo!r}')
        return None
    else:
        # RFC 3986 §3.1: grade a present scheme before using it for authority
        # or transport mapping.
        scheme = scheme_pseudo.lower()

    connect = method_is(method, 'CONNECT')
    if not connect:
        if PseudoHeaders.SCHEME not in frame.pseudo_headers:
            frame._mark_malformed('missing :scheme')
            return None
        if path_pseudo is None:
            frame._mark_malformed('missing :path')
            return None
        if path_pseudo == '':
            frame._mark_malformed('empty :path')
            return None

    protocol = frame.pseudo_headers.get(PseudoHeaders.PROTOCOL, '')

    if connect and protocol == 'websocket':
        # RFC 8441 §4 — Extended CONNECT bootstrapping WebSocket over HTTP/2.
        # ``method`` is the true wire value, never a placeholder: it IS read
        # for websocket-typed Connections, by ``AccessLogRecord.from_conn``
        # and by any global middleware whose method gate lacks a ``conn.type``
        # guard (``Cache``'s cacheable-methods check, for one).  Routing and
        # lifecycle events are unaffected — ``BlackBull._dispatch`` branches on
        # ``conn.type`` before any method-based dispatch.
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
            scheme='wss' if scheme == 'https' else 'ws',
            path=path, raw_path=raw_path, query_string=query_string,
            headers=Headers.from_lowered(raw_headers), http_version='2',
        )

    path, raw_path, query_string = '', b'', b''
    if p := frame.pseudo_headers.get(PseudoHeaders.PATH):
        path, raw_path, query_string = _split_h2_path(p)

    # Plain CONNECT is excluded from the host check: §8.5 gives its
    # ``:authority`` tunnel semantics, and the presence rule only binds
    # http/https requests.
    #
    # ``from_lowered`` is safe on every H/2 path: §8.2.1 makes an uppercase
    # field name malformed and ``HeadersFrame.parse_payload`` rejects the
    # frame before the pair reaches ``frame.headers``, so the list is
    # lowercase by protocol.  The injected ``host`` is a lowercase literal.
    if connect:
        headers = Headers.from_lowered(frame.headers)
    else:
        raw_headers = _request_headers_with_host(
            frame, require_present=scheme in ('http', 'https'))
        if raw_headers is None:
            return None
        headers = Headers.from_lowered(raw_headers)

    # ``root_path`` is NOT taken from the client-controlled X-Forwarded-Prefix;
    # only TrustedProxy sets it after verifying the peer.  Both branches leave
    # it at the field default ('') — the RFC-safe empty mount.
    return _build_h2_connection(method, path, raw_path,
                                query_string, headers, scheme)
