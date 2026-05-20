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

    await app._handle_lifespan({'type': 'lifespan'}, mock_receive, mock_send)

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

    await app._handle_lifespan({'type': 'lifespan'}, mock_receive, mock_send)

    assert not failed
