from urllib.parse import unquote, urlsplit

from ..protocol.frame_types import PseudoHeaders
import logging
from ..headers import Headers

logger = logging.getLogger(__name__)

_ASGI_VERSION: dict = {'version': '3.0', 'spec_version': '2.2'}


def _make_scope():
    return {
        'type': 'http',
        'asgi': _ASGI_VERSION,
        'http_version': '2',
        'method': 'HEAD',
        'scheme': 'https',
        'path': '',
        'raw_path': b'',
        'query_string': b'',
        'root_path': '',
        'headers': [],
        'client': [],
        'server': [],
        'state': {},
    }


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


def parse_headers(frame) -> dict:
    """Build an ASGI ``http`` (or ``websocket``) scope from a HEADERS frame.

    Hot path on every request — kept as a module-level function so that
    callers avoid the dict-lookup + parser allocation that ``ParserFactory``
    requires.

    Also performs request-level pseudo-header presence checks (RFC 9113
    §8.3.1).  Field-level checks already happened in ``parse_payload``; if
    that flagged ``frame.malformed`` we still build a scope to keep the
    contract simple but the actor will discard it before dispatch.  If
    parse_headers itself finds a missing or empty required pseudo, it sets
    ``frame.malformed`` so the same actor check rejects the request.
    """
    # Short-circuit if the frame parser already flagged this malformed.
    if getattr(frame, 'malformed', False):
        return _make_scope()

    # RFC 9113 §8.3.1 — ":status" is a response pseudo-header and MUST NOT
    # appear in a request.  ``parse_payload`` accepted it as a known pseudo-
    # header; we reject it here at the request layer.
    if PseudoHeaders.STATUS in frame.pseudo_headers:
        frame._mark_malformed('response pseudo-header in request: :status')
        return _make_scope()

    # RFC 9113 §8.3.1 — required request pseudo-headers.
    # CONNECT (RFC 9113 §8.5) omits :scheme and :path; the WebSocket
    # extension (RFC 8441) is detected below.
    method = frame.pseudo_headers.get(PseudoHeaders.METHOD)
    if method is None:
        frame._mark_malformed('missing :method')
        return _make_scope()
    if method != 'CONNECT':
        if PseudoHeaders.SCHEME not in frame.pseudo_headers:
            frame._mark_malformed('missing :scheme')
            return _make_scope()
        path = frame.pseudo_headers.get(PseudoHeaders.PATH)
        if path is None:
            frame._mark_malformed('missing :path')
            return _make_scope()
        if path == '':
            frame._mark_malformed('empty :path')
            return _make_scope()

    scope = _make_scope()

    protocol = frame.pseudo_headers.get(PseudoHeaders.PROTOCOL, '')

    if method == 'CONNECT' and protocol == 'websocket':
        # RFC 8441 §4 — Extended CONNECT bootstrapping WebSocket over HTTP/2
        scope['type'] = 'websocket'
        scheme = frame.pseudo_headers.get(PseudoHeaders.SCHEME, 'https')
        scope['scheme'] = 'wss' if scheme == 'https' else 'ws'
        if path := frame.pseudo_headers.get(PseudoHeaders.PATH):
            scope['path'], scope['raw_path'], scope['query_string'] = \
                _split_h2_path(path)
        scope['headers'] = Headers(frame.headers)
        # Bug 1.16 — root_path is NOT taken from the client-controlled
        # X-Forwarded-Prefix; only TrustedProxy sets it after verifying the
        # peer.  Default to the RFC-safe empty mount.
        scope['root_path'] = ''
        raw_sp = scope['headers'].get(b'sec-websocket-protocol', b'')
        scope['subprotocols'] = (
            [p.strip().decode('utf-8', errors='replace') for p in raw_sp.split(b',')]
            if raw_sp else [])
        return scope

    if method:
        scope['method'] = method

    if path := frame.pseudo_headers.get(PseudoHeaders.PATH):
        scope['path'], scope['raw_path'], scope['query_string'] = \
            _split_h2_path(path)

    if scheme := frame.pseudo_headers.get(PseudoHeaders.SCHEME):
        scope['scheme'] = scheme

    scope['headers'] = Headers(frame.headers)

    # Bug 1.16 — root_path is NOT taken from the client-controlled
    # X-Forwarded-Prefix; only TrustedProxy sets it after verifying the peer.
    scope['root_path'] = ''

    return scope
