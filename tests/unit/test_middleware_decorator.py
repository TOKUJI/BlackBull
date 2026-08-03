"""Tests for the @as_middleware decorator and _normalize_send utility."""
import json

import pytest

from blackbull.middleware.utils import _normalize_send, as_middleware
from blackbull.native import NativeResponse
from blackbull.response import JSONResponse, Response

# ---------------------------------------------------------------------------
# _normalize_send — native contract (Sprint 92): the middleware's inner send
# wrapper observes NativeResponse, never raw Response objects.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_normalize_response_to_single_native_response():
    sent = []

    async def inner(event, *a, **kw):
        sent.append(event)

    await _normalize_send(inner)(Response('hello'))

    assert len(sent) == 1
    n = sent[0]
    assert isinstance(n, NativeResponse)
    assert n.status == 200
    assert (b'content-type', b'text/html; charset=utf-8') in list(n.header)
    assert n.body == b'hello'


@pytest.mark.asyncio
async def test_normalize_json_response():
    sent = []

    async def inner(event, *a, **kw):
        sent.append(event)

    await _normalize_send(inner)(JSONResponse({'ok': True}))

    n = sent[0]
    assert isinstance(n, NativeResponse)
    assert json.loads(n.body) == {'ok': True}


@pytest.mark.asyncio
async def test_normalize_dict_converts_to_native():
    sent = []

    async def inner(event, *a, **kw):
        sent.append(event)

    event = {'type': 'http.response.start', 'status': 204, 'headers': []}
    await _normalize_send(inner)(event)

    assert len(sent) == 1
    n = sent[0]
    assert isinstance(n, NativeResponse)
    assert n.status == 204
    assert n.header is not None


# ---------------------------------------------------------------------------
# @as_middleware decorator — normalisation guarantee
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_decorated_inner_send_receives_native_for_response():
    """Inner send wrapper inside an @as_middleware-decorated function sees
    NativeResponse on the native contract (Sprint 92)."""
    received = []

    @as_middleware
    async def mw(scope, receive, send, call_next):
        async def inner_send(event):
            received.append(event)
            await send(event)
        await call_next(scope, receive, inner_send)

    async def handler(scope, receive, send):
        await send(Response('hi'))

    outer_sent = []

    async def outer_send(event, *a, **kw):
        outer_sent.append(event)

    await mw({}, None, outer_send, call_next=handler)

    assert all(isinstance(e, NativeResponse) for e in received)
    assert received[0].header is not None
    assert received[0].body == b'hi'


@pytest.mark.asyncio
async def test_decorated_inner_send_receives_native_for_json_response():
    received = []

    @as_middleware
    async def mw(scope, receive, send, call_next):
        async def inner_send(event):
            received.append(event)
            await send(event)
        await call_next(scope, receive, inner_send)

    async def handler(scope, receive, send):
        await send(JSONResponse({'n': 42}))

    async def noop_send(event, *a, **kw):
        pass

    await mw({}, None, noop_send, call_next=handler)

    assert all(isinstance(e, NativeResponse) for e in received)
    assert json.loads(received[0].body) == {'n': 42}


# ---------------------------------------------------------------------------
# Power-user contract: undecorated middleware call_next is NOT wrapped
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_undecorated_call_next_is_not_wrapped():
    captured = []

    async def raw_mw(scope, receive, send, call_next):
        captured.append(call_next)
        await call_next(scope, receive, send)

    async def handler(scope, receive, send):
        pass

    await raw_mw({}, None, None, call_next=handler)

    assert captured[0] is handler   # exact same object, no wrapper


# ---------------------------------------------------------------------------
# Decorator metadata
# ---------------------------------------------------------------------------

def test_marker_attribute_is_set():
    @as_middleware
    async def mw(scope, receive, send, call_next):
        pass

    assert getattr(mw, '__blackbull_middleware__', False) is True


def test_wraps_preserves_name_and_doc():
    @as_middleware
    async def my_middleware(scope, receive, send, call_next):
        """My doc."""

    assert my_middleware.__name__ == 'my_middleware'
    assert my_middleware.__doc__ == 'My doc.'


# ---------------------------------------------------------------------------
# Startup validation
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_startup_rejects_middleware_without_call_next():
    from blackbull import BlackBull

    app = BlackBull()

    async def bad_mw(scope, receive, send):   # missing call_next
        pass

    app.use(bad_mw)

    failed_messages = []

    async def mock_send(event):
        if event.get('type') == 'lifespan.startup.failed':
            failed_messages.append(event['message'])

    events = iter([{'type': 'lifespan.startup'}, {'type': 'lifespan.shutdown'}])

    async def mock_receive():
        return next(events)

    await app._handle_lifespan(mock_receive, mock_send)

    assert failed_messages
    assert 'call_next' in failed_messages[0]


@pytest.mark.asyncio
async def test_startup_accepts_undecorated_valid_middleware():
    from blackbull import BlackBull

    app = BlackBull()

    async def raw_mw(scope, receive, send, call_next):
        await call_next(scope, receive, send)

    app.use(raw_mw)

    failed = []

    async def mock_send(event):
        if event.get('type') == 'lifespan.startup.failed':
            failed.append(True)

    events = iter([{'type': 'lifespan.startup'}, {'type': 'lifespan.shutdown'}])

    async def mock_receive():
        return next(events)

    await app._handle_lifespan(mock_receive, mock_send)

    assert not failed


# ---------------------------------------------------------------------------
# Regression (0.43.2): plain (undecorated) middleware send wrappers must see
# ASGI dicts, never Response objects, when driven through the full app stack.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_h2_lane_normalization_stays_dict_with_as_middleware():
    """Clean-subagent BLOCKER guard: on an http_version='2' request,
    @as_middleware's normalisation must keep the ASGI-dict contract.  A
    leaked NativeResponse would TypeError the H2 sender (no native arm yet —
    the H2 gate)."""
    from blackbull import BlackBull
    from blackbull.headers import Headers
    from blackbull.connection import Connection

    app = BlackBull()

    @as_middleware
    async def forward_mw(scope, receive, send, call_next):
        async def inner_send(event):
            await send(event)
        await call_next(scope, receive, inner_send)

    app.use(forward_mw)

    @app.route(path='/h2')
    async def h2():
        return b'ok'

    conn = Connection(type='http', http_version='2', method='GET', scheme='http',
                      path='/h2', raw_path=b'/h2', query_string=b'', root_path='',
                      headers=Headers([(b'host', b'localhost')]),
                      client=('127.0.0.1', 5), server=('localhost', 80), extensions={})
    sent = []

    async def receive():
        return {'type': 'http.request', 'body': b'', 'more_body': False}

    async def send(ev):
        sent.append(ev)

    await app(conn, receive, send)

    assert sent, 'no response emitted'
    assert all(isinstance(e, dict) for e in sent), (
        f'NativeResponse leaked onto the H2 lane: {sent!r}')
    assert any(e.get('type') == 'http.response.start' for e in sent)


@pytest.mark.asyncio
async def test_h2_with_cors_global_middleware_stays_dict():
    """Clean-subagent BLOCKER: H2 + global CORS must not leak NativeResponse
    to the H2 sender (would TypeError — HTTP2Sender has no native arm)."""
    from blackbull import BlackBull
    from blackbull.headers import Headers
    from blackbull.connection import Connection
    from blackbull.middleware.cors import CORS

    app = BlackBull()
    app.use(CORS(allow_origins=['https://example.com']))

    @app.route(path='/h2c')
    async def h2c():
        return {'ok': True}

    conn = Connection(type='http', http_version='2', method='GET', scheme='http',
                      path='/h2c', raw_path=b'/h2c', query_string=b'', root_path='',
                      headers=Headers([(b'host', b'localhost'),
                                       (b'origin', b'https://example.com')]),
                      client=('127.0.0.1', 5), server=('localhost', 80), extensions={})
    sent = []

    async def receive():
        return {'type': 'http.request', 'body': b'', 'more_body': False}

    async def send(ev):
        sent.append(ev)

    await app(conn, receive, send)

    assert all(isinstance(e, dict) for e in sent), (
        f'NativeResponse leaked to the H2 lane via CORS: {sent!r}')
    start = next(e for e in sent if e.get('type') == 'http.response.start')
    hdrs = dict(start.get('headers', []))
    assert hdrs.get(b'access-control-allow-origin') == b'https://example.com'


@pytest.mark.asyncio
async def test_plain_middleware_send_wrapper_sees_native():
    """An undecorated middleware that wraps ``send`` must receive
    NativeResponse on the H1 native path — never raw ``Response`` objects.

    Before 0.43.2 ``_wrap_send`` was applied at ``BlackBull.__call__``
    (outermost), so a simplified handler returning a dict (auto-JSONResponse)
    reached the middleware's send wrapper as a ``Response`` object →
    ``TypeError: 'Response' object is not subscriptable``.  The adapter now
    sits at the handler boundary in ``_dispatch`` so middleware sees the
    native contract (NativeResponse on H1; ASGI dicts on H2 / the ASGI
    path), never a bare ``Response``.  The drive below enters via a scope
    dict, so the external edge converts the native emission back to dicts
    for the host's ``send``.
    """
    from blackbull import BlackBull
    from blackbull.native import NativeResponse

    app = BlackBull()
    seen_status = []

    async def stats_mw(scope, receive, send, call_next):
        async def capture(msg):
            if isinstance(msg, NativeResponse):
                if msg.header is not None:        # native header arm
                    seen_status.append(msg.status)
            elif msg.get('type') == 'http.response.start':  # H2/ASGI lane
                seen_status.append(msg['status'])
            await send(msg)
        await call_next(scope, receive, capture)

    app.use(stats_mw)

    @app.route(path='/health')
    async def health():
        return {'status': 'ok'}          # auto JSONResponse

    sent = []
    scope = {'type': 'http', 'method': 'GET', 'path': '/health',
             'headers': [], 'client': ('127.0.0.1', 1)}

    async def receive():
        return {'type': 'http.request', 'body': b'', 'more_body': False}

    async def send(event):
        sent.append(event)

    await app(scope, receive, send)

    assert seen_status == [200]
    # scope entry → the external edge converts native → ASGI dicts
    assert all(isinstance(e, dict) for e in sent)
    assert sent[0]['type'] == 'http.response.start'
