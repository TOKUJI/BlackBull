"""Tests for the @as_middleware decorator and _normalize_send utility."""
import pytest

from blackbull.middleware.utils import _normalize_send, as_middleware
from blackbull.response import JSONResponse, Response


# ---------------------------------------------------------------------------
# _normalize_send
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_normalize_response_expands_to_start_and_body():
    sent = []

    async def inner(event, *a, **kw):
        sent.append(event)

    await _normalize_send(inner)(Response('hello'))

    assert sent[0] == {
        'type': 'http.response.start',
        'status': 200,
        'headers': [(b'content-type', b'text/html; charset=utf-8')],
    }
    assert sent[1]['type'] == 'http.response.body'
    assert sent[1]['body'] == b'hello'
    assert sent[1]['more_body'] is False


@pytest.mark.asyncio
async def test_normalize_json_response():
    import json
    sent = []

    async def inner(event, *a, **kw):
        sent.append(event)

    await _normalize_send(inner)(JSONResponse({'ok': True}))

    assert sent[0]['type'] == 'http.response.start'
    assert json.loads(sent[1]['body']) == {'ok': True}


@pytest.mark.asyncio
async def test_normalize_dict_passthrough():
    sent = []

    async def inner(event, *a, **kw):
        sent.append(event)

    event = {'type': 'http.response.start', 'status': 204, 'headers': []}
    await _normalize_send(inner)(event)

    assert sent == [event]


# ---------------------------------------------------------------------------
# @as_middleware decorator — normalisation guarantee
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_decorated_inner_send_receives_dicts_for_response():
    """Inner send wrapper inside an @as_middleware-decorated function sees only dicts."""
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

    assert all(isinstance(e, dict) for e in received)
    assert received[0]['type'] == 'http.response.start'
    assert received[1]['type'] == 'http.response.body'


@pytest.mark.asyncio
async def test_decorated_inner_send_receives_dicts_for_json_response():
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

    assert all(isinstance(e, dict) for e in received)


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
        pass

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
async def test_plain_middleware_send_wrapper_sees_asgi_dicts():
    """An undecorated middleware that wraps ``send`` and subscripts
    ``msg['type']`` must receive ASGI dicts.

    Before 0.43.2 ``_wrap_send`` was applied at ``BlackBull.__call__``
    (outermost), so a simplified handler returning a dict (auto-JSONResponse)
    reached the middleware's send wrapper as a ``Response`` object →
    ``TypeError: 'Response' object is not subscriptable``.  The adapter now
    sits at the handler boundary in ``_dispatch`` so middleware always sees
    plain dicts.
    """
    from blackbull import BlackBull

    app = BlackBull()
    seen_status = []

    async def stats_mw(scope, receive, send, call_next):
        async def capture(msg):
            if msg['type'] == 'http.response.start':   # subscripts the dict
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
    assert all(isinstance(e, dict) for e in sent)
    assert sent[0]['type'] == 'http.response.start'
