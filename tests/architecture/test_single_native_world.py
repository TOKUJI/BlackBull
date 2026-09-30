"""Architecture guard: one native world, with the ASGI boundaries enumerated.

BlackBull threads a native ``Connection`` in and a ``NativeResponse`` out.  The
ASGI dict form is a *boundary encoding*, not an internal one: it exists where
BlackBull meets something that speaks ASGI, and nowhere else.  Every conversion
in the framework is therefore either one of those boundaries or a defect.

The failure mode this catches is not a crash — it is silent cost.  A framework
producer that emits ``{'type': 'http.response.start', ...}`` still *works*,
because the boundary adapters convert it back; it just pays for a round trip
that has no reason to exist, and it re-creates the second conversion altitude
whose removal is the point of the single-world policy.  Nothing fails, no test
goes red, and the seam grows back one producer at a time.  So the rule is
enforced structurally instead: every conversion site is named here with the
reason it is allowed to exist.

Each allowlist entry is ``'<module>::<qualname>'`` mapped to that reason.  An
entry reading *boundary* is permanent — it is where BlackBull meets ASGI.  An
entry reading *residual* is a producer that has not been converted yet: still
correct, still paying the round trip, and the list of them is the remaining
work.  Adding a new *boundary* entry means claiming a new external edge exists;
adding a new *residual* entry means moving in the wrong direction.
"""
import ast
import pathlib

import pytest


_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
_PKG = _REPO_ROOT / 'blackbull'

# ---------------------------------------------------------------------------
# Allowlists
# ---------------------------------------------------------------------------

# ``NativeResponse.to_asgi()`` — the native → ASGI expansion.  Every entry is a
# boundary: there is no residual caller left.
_TO_ASGI_ALLOWED: dict[str, str] = {
    'blackbull/native.py::asgi_send_boundary._send':
        'boundary — the shared send-side expansion both ASGI edges call; the '
        'edges themselves are enumerated in _BOUNDARY_WRAPPER_ALLOWED below',
    'blackbull/testing/native.py::request.send':
        'boundary — Tier-1 builds the documented NativeResponse.events surface '
        '(docs/guide/testing.md); reusing to_asgi() keeps one expansion',
    'blackbull/testing/__init__.py::WebSocketTestSession._recv_event':
        'boundary — Tier-2 drives the app through the ASGI WebSocket contract; '
        'the one place server events enter the client',
}

# Who may wrap a ``send`` with the native → ASGI expansion.  The expansion
# itself moved into one helper, so without this the edges would stop being
# enumerated: any module could acquire a boundary by calling it.
_BOUNDARY_WRAPPER_ALLOWED: dict[str, str] = {
    'blackbull/app.py::_to_asgi_boundary':
        'boundary — the external host edge: BlackBull(asgi=True) under uvicorn',
    'blackbull/middleware/utils.py::_to_asgi_send':
        'boundary — a @as_middleware middleware that declared `scope`; the '
        'dicts exist across that one frame and are native again on both sides',
}

# Response-event dict literals (``{'type': 'http.response.*', ...}``) built by
# framework code.  A native producer emits a ``NativeResponse`` instead.
_RESPONSE_DICT_ALLOWED: dict[str, str] = {
    'blackbull/native.py::NativeResponse.to_asgi':
        'boundary — this is the conversion the boundaries above call',
    'blackbull/testing/native.py::request._record':
        'boundary — Tier-1 assembles the .events surface here',

    # No residual response-dict producers remain: every framework-owned
    # emitter on the native path builds a NativeResponse.  An entry added here
    # without the word "boundary" is a regression.
}

# ``Connection.to_asgi_scope()`` — the native → ASGI request-side conversion.
_TO_ASGI_SCOPE_ALLOWED: dict[str, str] = {
    'blackbull/server/http1_actor.py::RequestActor.run':
        'boundary — BB_FORCE_ASGI_SCOPE=1, the dual-path conformance lane. '
        'The run method owns the boundary: it answers "what is the app called '
        'with" (inlined for the hot path) and is shared by H/1 and H/2, so a '
        'scope can come into existence here and nowhere else on either '
        'protocol path',
    'blackbull/middleware/utils.py::_adapt':
        'boundary — builds the scope a `scope`-declaring middleware asked for',
}

# Request-event dict literals (``{'type': 'http.request', ...}``).  The body
# crosses the framework as ``bytes`` (``next_chunk``); the dict is the ASGI
# *encoding* of it, and only a caller that wants that encoding should pay.
_REQUEST_DICT_ALLOWED: dict[str, str] = {
    'blackbull/server/recipient.py::HTTP1Recipient.__call__':
        'boundary — the ASGI receive channel, minted per call for whoever '
        'asked for it (a full-form receive(), or an external host)',
    'blackbull/server/recipient.py::HTTP2Recipient.__call__':
        'boundary — the same channel on H2',
    'blackbull/app.py::BlackBull.warm_request._receive':
        'boundary — a synthetic ASGI receive built for warm-up requests; it '
        'is a plain callable with no native arm, by construction',
    'blackbull/testing/native.py::request.receive':
        'boundary — Tier-1 drives the app through the ASGI receive contract',
}

# WebSocket event dict literals.  Both halves are native now — the send side
# carries ``NativeWSMessage``, the receive side carries the message itself
# (``str`` text, ``bytes`` binary) — so every entry here is a boundary.
_WS_DICT_ALLOWED: dict[str, str] = {
    'blackbull/native.py::NativeWSMessage.to_asgi':
        'boundary — this is the conversion',
    'blackbull/testing/__init__.py::WebSocketTestSession.__enter__':
        'boundary — Tier-2 speaks the ASGI WebSocket contract to the app',
    'blackbull/testing/__init__.py::WebSocketTestSession.send_text':
        'boundary — Tier-2 client → app',
    'blackbull/testing/__init__.py::WebSocketTestSession.send_bytes':
        'boundary — Tier-2 client → app',
    'blackbull/testing/__init__.py::WebSocketTestSession.close':
        'boundary — Tier-2 client → app',
    'blackbull/response.py::WebSocketResponse':
        'boundary — the documented ASGI-event builder for the raw '
        '(conn, receive, send) WebSocket form',
    'blackbull/middleware/websocket.py::<module>':
        'boundary — the `websocket` middleware consumes the raw connect event '
        'on behalf of a raw-form handler',
    'blackbull/server/recipient.py::WebSocketRecipient.__call__':
        'boundary — the ASGI receive channel, minted per call for the raw '
        '(conn, receive, send) form and the external host; the object form '
        'takes next_message() and pays nothing',
    'blackbull/server/websocket_actor.py::WebSocketActor._emit_websocket_message':
        "boundary — builds the websocket_message event's documented "
        "{'conn', 'text', 'bytes'} detail, and only when a listener wants it",
}

# The ASGI → native response parser and its typed event shapes.  These exist
# for the external compat surface; a middleware importing one is a middleware
# reading the dict form, which is the lane the single-world policy removed.
_DICT_LANE_NAMES = frozenset({
    'parse_response_event', 'ResponseStart', 'ResponseBody',
})

_RESPONSE_TYPES = frozenset({
    'http.response.start', 'http.response.body',
    'http.response.trailers', 'http.response.pathsend',
})
_RESPONSE_TYPE_ATTRS = frozenset({
    'HTTP_RESPONSE_START', 'HTTP_RESPONSE_BODY',
    'HTTP_RESPONSE_TRAILERS', 'HTTP_RESPONSE_PATHSEND',
})
_REQUEST_TYPES = frozenset({'http.request'})
_REQUEST_TYPE_ATTRS = frozenset({'HTTP_REQUEST'})
_WS_TYPES = frozenset({
    'websocket.accept', 'websocket.send', 'websocket.close',
    'websocket.connect', 'websocket.receive', 'websocket.disconnect',
})
_WS_TYPE_ATTRS = frozenset({
    'WS_ACCEPT', 'WS_SEND', 'WS_CLOSE',
    'WS_CONNECT', 'WS_RECEIVE', 'WS_DISCONNECT',
})


# ---------------------------------------------------------------------------
# Scanner
# ---------------------------------------------------------------------------

class _Scanner(ast.NodeVisitor):
    """Collects conversion sites tagged with their enclosing qualname."""

    def __init__(self, module: str) -> None:
        self.module = module
        self._stack: list[str] = []
        self.to_asgi: list[str] = []
        self.response_dicts: list[str] = []
        self.to_asgi_scope: list[str] = []
        self.request_dicts: list[str] = []
        self.ws_dicts: list[str] = []
        self.dict_lane_imports: list[str] = []
        self.boundary_wrappers: list[str] = []

    # -- qualname tracking --------------------------------------------------

    @property
    def _site(self) -> str:
        return f'{self.module}::{".".join(self._stack) or "<module>"}'

    def _scoped(self, node) -> None:
        self._stack.append(node.name)
        self.generic_visit(node)
        self._stack.pop()

    visit_FunctionDef = visit_AsyncFunctionDef = visit_ClassDef = _scoped

    # -- the four checks ----------------------------------------------------

    def visit_Call(self, node: ast.Call) -> None:
        func = node.func
        if isinstance(func, ast.Attribute):
            if func.attr == 'to_asgi':
                self.to_asgi.append(self._site)
            elif func.attr == 'to_asgi_scope':
                self.to_asgi_scope.append(self._site)
        elif isinstance(func, ast.Name) and func.id == 'asgi_send_boundary':
            self.boundary_wrappers.append(self._site)
        self.generic_visit(node)

    def visit_Dict(self, node: ast.Dict) -> None:
        for key, value in zip(node.keys, node.values):
            if not (isinstance(key, ast.Constant) and key.value == 'type'):
                continue
            if isinstance(value, ast.Constant) and value.value in _RESPONSE_TYPES:
                self.response_dicts.append(self._site)
            elif (isinstance(value, ast.Attribute)
                    and value.attr in _RESPONSE_TYPE_ATTRS):
                self.response_dicts.append(self._site)
            elif isinstance(value, ast.Constant) and value.value in _REQUEST_TYPES:
                self.request_dicts.append(self._site)
            elif (isinstance(value, ast.Attribute)
                    and value.attr in _REQUEST_TYPE_ATTRS):
                self.request_dicts.append(self._site)
            elif isinstance(value, ast.Constant) and value.value in _WS_TYPES:
                self.ws_dicts.append(self._site)
            elif (isinstance(value, ast.Attribute)
                    and value.attr in _WS_TYPE_ATTRS):
                self.ws_dicts.append(self._site)
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        for alias in node.names:
            if alias.name in _DICT_LANE_NAMES:
                self.dict_lane_imports.append(f'{self.module}::{alias.name}')
        self.generic_visit(node)


def _scan_package() -> list[_Scanner]:
    scanners = []
    for path in sorted(_PKG.rglob('*.py')):
        module = path.relative_to(_REPO_ROOT).as_posix()
        scanner = _Scanner(module)
        scanner.visit(ast.parse(path.read_text(encoding='utf-8')))
        scanners.append(scanner)
    return scanners


@pytest.fixture(scope='module')
def scanned() -> list[_Scanner]:
    return _scan_package()


def _report(found: set[str], allowed: dict[str, str], what: str) -> str:
    unlisted = sorted(found - set(allowed))
    stale = sorted(set(allowed) - found)
    lines = []
    if unlisted:
        lines.append(
            f'{what} at unenumerated sites — emit native, or add the site to '
            f'the allowlist with the boundary it crosses:')
        lines += [f'    {s}' for s in unlisted]
    if stale:
        lines.append(f'stale allowlist entries (site is gone — delete them):')
        lines += [f'    {s}' for s in stale]
    return '\n'.join(lines)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.parametrize('attr,allowed,label', [
    pytest.param('to_asgi', _TO_ASGI_ALLOWED, 'native → ASGI expansion',
                 id='to-asgi-boundaries'),
    pytest.param('boundary_wrappers', _BOUNDARY_WRAPPER_ALLOWED, 'ASGI send-boundary wrap',
                 id='asgi-send-boundary'),
    pytest.param('response_dicts', _RESPONSE_DICT_ALLOWED, 'ASGI response-event dict',
                 id='response-dicts'),
    pytest.param('request_dicts', _REQUEST_DICT_ALLOWED, 'ASGI request-event dict',
                 id='request-dicts'),
    pytest.param('ws_dicts', _WS_DICT_ALLOWED, 'ASGI websocket-event dict',
                 id='ws-dicts'),
    pytest.param('to_asgi_scope', _TO_ASGI_SCOPE_ALLOWED, 'Connection → ASGI scope conversion',
                 id='to-asgi-scope'),
])
def test_request_dicts_only_at_enumerated_boundaries(scanned, attr, allowed, label):
    """Native/ASGI conversions happen only at the enumerated boundaries:
    each rule is found − allowed over its scanner attribute."""
    found = {s for sc in scanned for s in getattr(sc, attr)}
    problem = _report(found, allowed, label)
    assert not problem, problem


def test_the_scan_still_matches_something(scanned):
    """An allowlist guard that matches nothing passes for the wrong reason.

    Every rule above is ``found - allowed``, so a scanner that stopped
    recognising its pattern — a renamed event constant, a changed AST shape —
    would go green while the invariant rotted.  Each rule must keep finding the
    boundaries it was written to enumerate.
    """
    assert {s for sc in scanned for s in sc.to_asgi}
    assert {s for sc in scanned for s in sc.response_dicts}
    assert {s for sc in scanned for s in sc.to_asgi_scope}
    assert {s for sc in scanned for s in sc.request_dicts}
    assert {s for sc in scanned for s in sc.ws_dicts}


def test_the_scan_catches_a_new_dict_producer():
    """A middleware that grows a dict producer is reported, not tolerated."""
    offender = _Scanner('blackbull/middleware/regression.py')
    offender.visit(ast.parse(
        'async def mw(conn, receive, send, call_next):\n'
        "    await send({'type': 'http.response.start', 'status': 200,\n"
        "                'headers': []})\n"
        "    await send({'type': 'http.response.body', 'body': b''})\n"
    ))
    assert offender.response_dicts == [
        'blackbull/middleware/regression.py::mw',
        'blackbull/middleware/regression.py::mw',
    ]
    assert _report(set(offender.response_dicts), _RESPONSE_DICT_ALLOWED, 'x')


def test_middleware_never_imports_the_dict_lane(scanned):
    """No middleware reads responses through the ASGI dict shapes.

    ``parse_response_event`` / ``ResponseStart`` / ``ResponseBody`` are the
    external compat vocabulary.  A middleware importing one has grown a second
    lane through itself — the exact shape deleted from ``Compression``.
    """
    offenders = sorted(
        name for sc in scanned for name in sc.dict_lane_imports
        if sc.module.startswith('blackbull/middleware/'))
    assert not offenders, (
        'middleware importing the ASGI dict lane:\n'
        + '\n'.join(f'    {o}' for o in offenders))
