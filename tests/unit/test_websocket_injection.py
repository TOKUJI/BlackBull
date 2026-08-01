"""Signature injection into WebSocket handlers.

A WebSocket handler gets its object; this covers what the *signature*
may declare alongside it — path params, query params, and ``Depends`` — plus
the registration-time errors that keep an unresolvable parameter loud.

Two layers, same split as ``test_websocket_object.py``: the plan (classified
once, at registration) and the wrapper (which only indexes it). The dependency
**lifetime** contract — resolved once per connection, torn down when the
handler exits by any route — is pinned here rather than e2e because the exit
routes are easiest to drive deterministically against a scripted channel.
"""
import pytest

from blackbull import (BlackBull, Connection, Depends, Headers, WebSocket,
                       WebSocketDisconnect)
from blackbull.router import (_ParamKind, _adapt_websocket_handler,
                              _websocket_param_plan)
from blackbull.utils import Scheme


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------

class _Channel:
    """A scripted receive channel plus a recording send channel."""

    def __init__(self, *inbound):
        self._inbound = list(inbound)
        self.sent = []

    async def receive(self):
        if not self._inbound:
            return {'type': 'websocket.disconnect', 'code': 1006}
        return self._inbound.pop(0)

    async def send(self, event, *_args, **_kwargs):
        self.sent.append(event)


CONNECT = {'type': 'websocket.connect'}


def _conn(path='/ws', *, query=b'', path_params=None) -> Connection:
    conn = Connection(method='GET', path=path, raw_path=path.encode(),
                      headers=Headers([]), type='websocket', query_string=query)
    if path_params:
        conn.path_params = dict(path_params)
    return conn


def _kinds(plan) -> tuple[_ParamKind, ...]:
    return tuple(kind for _, kind, _ in plan)


def _closes(channel) -> list[dict]:
    return [e for e in channel.sent if e['type'] == 'websocket.close']


# ---------------------------------------------------------------------------
# Registration-time classification
# ---------------------------------------------------------------------------

def test_path_param_is_classified_by_name():
    async def handler(ws: WebSocket, room: str):
        pass

    assert _kinds(_websocket_param_plan(handler, {'room'})) == (_ParamKind.WS, _ParamKind.PATH)


def test_annotated_leftover_is_a_query_param():
    async def handler(ws: WebSocket, since: int):
        pass

    assert _kinds(_websocket_param_plan(handler, set())) == (_ParamKind.WS, _ParamKind.QUERY)


def test_query_param_must_be_annotated():
    """The one place the WebSocket plan is deliberately stricter than HTTP.

    HTTP takes a bare leftover name as a ``str`` query param, which is
    load-bearing there (``async def search(q)``). On a WebSocket the reserved
    names make bare parameters ambiguous — ``chat(socket)`` means the socket —
    so the annotation is required and a typo stays a registration error.
    """
    async def handler(ws: WebSocket, since):
        pass

    with pytest.raises(TypeError, match='must carry its annotation'):
        _websocket_param_plan(handler, set())


def test_depends_default_is_classified():
    async def provider():
        return 'db'

    async def handler(ws: WebSocket, db=Depends(provider)):
        pass

    assert _kinds(_websocket_param_plan(handler, set())) == (_ParamKind.WS, _ParamKind.DEPENDS)


@pytest.mark.parametrize('name', ['ws', 'websocket', 'conn', 'connection'])
def test_depends_on_a_reserved_name_is_rejected(name):
    """Silently ignoring the Depends would hand back the socket instead."""
    async def provider():
        return 'x'

    fn = eval(f'lambda {name}=Depends(provider): None', {'Depends': Depends,
                                                         'provider': provider})
    with pytest.raises(TypeError, match='reserved'):
        _websocket_param_plan(fn, set())


def test_depends_on_a_path_param_is_rejected():
    async def provider():
        return 'x'

    async def handler(ws: WebSocket, room=Depends(provider)):
        pass

    with pytest.raises(TypeError, match='path param'):
        _websocket_param_plan(handler, {'room'})


def test_body_shaped_annotation_is_rejected_with_a_websocket_specific_reason():
    """A WebSocket has no request body, so the HTTP body/dataclass categories
    have no analogue — say so rather than failing as 'bad query param'."""
    async def handler(ws: WebSocket, payload: dict):
        pass

    with pytest.raises(TypeError, match='no request body'):
        _websocket_param_plan(handler, set())


def test_unresolvable_parameter_still_fails_at_registration_through_the_router():
    app = BlackBull()

    with pytest.raises(TypeError, match='cannot resolve parameter'):
        @app.route(path='/rooms/{room}', scheme=Scheme.websocket)
        async def bad(ws: WebSocket, room: str, mystery: dict):  # noqa: F841
            pass


# ---------------------------------------------------------------------------
# The ws-only fast path is untouched
# ---------------------------------------------------------------------------

def test_lone_ws_still_compiles_to_the_fast_path_wrapper():
    """``@wraps`` rewrites ``__name__``, so the code object's own name is what
    identifies which wrapper was built."""
    async def handler(ws: WebSocket):
        pass

    adapted = _adapt_websocket_handler(handler, '/ws')
    assert adapted.__code__.co_name == '_ws_wrapper'


def test_any_injection_moves_to_the_plan_wrapper():
    async def handler(ws: WebSocket, room: str):
        pass

    adapted = _adapt_websocket_handler(handler, '/rooms/{room}')
    assert adapted.__code__.co_name == '_ws_plan_wrapper'


# ---------------------------------------------------------------------------
# Runtime binding
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_path_param_is_injected_and_coerced():
    seen = {}

    async def handler(ws: WebSocket, room: str, seat: int):
        seen['room'], seen['seat'] = room, seat

    adapted = _adapt_websocket_handler(handler, '/rooms/{room}/{seat}')
    channel = _Channel(CONNECT)
    await adapted(_conn(path_params={'room': 'lobby', 'seat': '12'}),
                  channel.receive, channel.send)

    assert seen == {'room': 'lobby', 'seat': 12}


@pytest.mark.asyncio
async def test_query_param_is_injected_with_coercion_and_default():
    seen = {}

    async def handler(ws: WebSocket, since: int, tok: str | None = None):
        seen['since'], seen['tok'] = since, tok

    adapted = _adapt_websocket_handler(handler, '/ws')
    channel = _Channel(CONNECT)
    await adapted(_conn(query=b'since=7'), channel.receive, channel.send)

    assert seen == {'since': 7, 'tok': None}


@pytest.mark.asyncio
async def test_missing_required_query_param_rejects_the_handshake():
    called = False

    async def handler(ws: WebSocket, since: int):
        nonlocal called
        called = True

    adapted = _adapt_websocket_handler(handler, '/ws')
    channel = _Channel(CONNECT)
    await adapted(_conn(), channel.receive, channel.send)

    assert not called, 'handler must not run when a declared param cannot bind'
    assert [e['code'] for e in _closes(channel)] == [1008]


@pytest.mark.asyncio
async def test_uncoercible_query_param_rejects_the_handshake():
    called = False

    async def handler(ws: WebSocket, since: int):
        nonlocal called
        called = True

    adapted = _adapt_websocket_handler(handler, '/ws')
    channel = _Channel(CONNECT)
    await adapted(_conn(query=b'since=soon'), channel.receive, channel.send)

    assert not called
    assert [e['code'] for e in _closes(channel)] == [1008]


@pytest.mark.asyncio
async def test_rejection_never_resolves_a_dependency():
    """A connection we are about to refuse must not acquire a resource first."""
    log = []

    async def provider():
        log.append('setup')
        try:
            yield 'db'
        finally:
            log.append('teardown')

    async def handler(ws: WebSocket, since: int, db=Depends(provider)):
        log.append('handler')

    adapted = _adapt_websocket_handler(handler, '/ws')
    channel = _Channel(CONNECT)
    await adapted(_conn(), channel.receive, channel.send)

    assert log == []
    assert [e['code'] for e in _closes(channel)] == [1008]


# ---------------------------------------------------------------------------
# Dependency lifetime: once per connection, torn down on every exit route
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_depends_teardown_runs_on_clean_return():
    log = []

    async def provider():
        log.append('setup')
        yield 'db'
        log.append('teardown')

    with pytest.warns(UserWarning, match='bare `yield`'):
        async def handler(ws: WebSocket, db=Depends(provider)):
            log.append(f'handler:{db}')

    adapted = _adapt_websocket_handler(handler, '/ws')
    channel = _Channel(CONNECT)
    await adapted(_conn(), channel.receive, channel.send)

    assert log == ['setup', 'handler:db', 'teardown']


@pytest.mark.asyncio
async def test_depends_teardown_runs_on_abrupt_disconnect():
    """The gate: teardown must not be tied to a *clean* close.

    ``WebSocketDisconnect`` propagating out of the handler is what an abrupt
    peer loss looks like from inside one. Note the ``try/finally`` — see
    ``test_bare_yield_provider_does_not_tear_down_on_an_exception`` for why
    that is the provider author's job and not something the framework fakes.
    """
    log = []

    async def provider():
        log.append('setup')
        try:
            yield 'db'
        finally:
            log.append('teardown')

    async def handler(ws: WebSocket, db=Depends(provider)):
        raise WebSocketDisconnect(1006, None)

    adapted = _adapt_websocket_handler(handler, '/ws')
    channel = _Channel(CONNECT)
    with pytest.raises(WebSocketDisconnect):
        await adapted(_conn(), channel.receive, channel.send)

    assert log == ['setup', 'teardown']


@pytest.mark.asyncio
async def test_depends_teardown_runs_when_the_handler_raises():
    log = []

    async def provider():
        log.append('setup')
        try:
            yield 'db'
        finally:
            log.append('teardown')

    async def handler(ws: WebSocket, db=Depends(provider)):
        raise RuntimeError('boom')

    adapted = _adapt_websocket_handler(handler, '/ws')
    channel = _Channel(CONNECT)
    with pytest.raises(RuntimeError, match='boom'):
        await adapted(_conn(), channel.receive, channel.send)

    assert log == ['setup', 'teardown']


@pytest.mark.asyncio
async def test_bare_yield_provider_does_not_tear_down_on_an_exception():
    """Pinned because it is the footgun, not because it is desirable.

    Cleanup written *after* a bare ``yield`` never runs when an exception is
    thrown into the generator — the exception surfaces at the yield and
    propagates. This is ``@asynccontextmanager`` semantics, identical on the
    HTTP path, and a WebSocket makes it bite harder because a socket ends by
    exception far more often than a request does. The docs tell providers to
    use ``try/finally``; this test is what makes that instruction load-bearing
    rather than advisory.
    """
    log = []

    async def provider():
        log.append('setup')
        yield 'db'
        log.append('teardown')      # deliberately unreachable on the raise path

    with pytest.warns(UserWarning, match='bare `yield`'):
        async def handler(ws: WebSocket, db=Depends(provider)):
            raise RuntimeError('boom')

    adapted = _adapt_websocket_handler(handler, '/ws')
    channel = _Channel(CONNECT)
    with pytest.raises(RuntimeError, match='boom'):
        await adapted(_conn(), channel.receive, channel.send)

    assert log == ['setup'], 'a bare-yield provider skips cleanup on exception'


@pytest.mark.asyncio
async def test_dependency_is_resolved_once_per_connection_not_per_message():
    """The rule — object per connection — governs dependencies too."""
    setups = []

    async def provider():
        setups.append(1)
        return object()

    async def handler(ws: WebSocket, db=Depends(provider)):
        await ws.accept()
        async for _ in ws:
            pass

    adapted = _adapt_websocket_handler(handler, '/ws')
    channel = _Channel(CONNECT,
                       {'type': 'websocket.receive', 'text': 'a', 'bytes': None},
                       {'type': 'websocket.receive', 'text': 'b', 'bytes': None},
                       {'type': 'websocket.disconnect', 'code': 1000})
    await adapted(_conn(), channel.receive, channel.send)

    assert len(setups) == 1


@pytest.mark.asyncio
async def test_cached_dependency_is_shared_between_parameters():
    calls = []

    async def provider():
        calls.append(1)
        return 'shared'

    async def handler(ws: WebSocket, a=Depends(provider), b=Depends(provider)):
        assert a is b

    adapted = _adapt_websocket_handler(handler, '/ws')
    channel = _Channel(CONNECT)
    await adapted(_conn(), channel.receive, channel.send)

    assert len(calls) == 1


@pytest.mark.asyncio
async def test_everything_together():
    """The signature the feature set out to make work."""
    seen = {}

    async def get_db():
        yield 'DB'

    async def chat(ws: WebSocket, conn: Connection, room: str,
                   since: int = 0, db=Depends(get_db)):
        seen.update(room=room, since=since, db=db, conn=conn, ws=ws)

    adapted = _adapt_websocket_handler(chat, '/rooms/{room}')
    conn = _conn(path='/rooms/lobby', query=b'since=7',
                 path_params={'room': 'lobby'})
    channel = _Channel(CONNECT)
    await adapted(conn, channel.receive, channel.send)

    assert seen['room'] == 'lobby'
    assert seen['since'] == 7
    assert seen['db'] == 'DB'
    assert seen['conn'] is conn
    assert seen['ws'].connection is conn
