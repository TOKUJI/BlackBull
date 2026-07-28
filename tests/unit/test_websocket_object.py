"""The high-level :class:`~blackbull.websocket.WebSocket` handler object (Sprint 82).

Two layers are covered here:

- the object itself, driven against a scripted receive/send pair, so each
  method's event output is pinned exactly; and
- registration, so the router's choice between the object form and the raw
  ``(conn, receive, send)`` form is pinned at the point it is made.

End-to-end behaviour over a real socket is covered by the TestClient tests in
``test_websocket_object_e2e.py`` — the point of *this* file is that a failure
localises to the wrapper rather than to the actor beneath it.
"""
import http
import json

import pytest

from blackbull import (BlackBull, Connection, Headers, WebSocket,
                       WebSocketDisconnect)
from blackbull.router import _adapt_websocket_handler, _websocket_param_plan
from blackbull.utils import Scheme

# ``send()`` annotates its parameter, so under the beartype import-hook the
# annotation is enforced *before* the method's own dispatch runs and the
# violation arrives as BeartypeCallHintParamViolation instead of the
# hand-written TypeError.  Both are correct rejections; which one surfaces
# depends on whether beartype is instrumenting.  Same optional-import dance
# as tests/unit/test_router.py.
try:
    from beartype.roar import BeartypeCallHintParamViolation as _BeartypeViolation
except ImportError:
    _BeartypeViolation = None  # type: ignore[assignment,misc]

_TYPE_ERRORS = (TypeError,) if _BeartypeViolation is None else (TypeError, _BeartypeViolation)


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
            # Nothing scripted left: behave like a peer that went away rather
            # than hanging the test forever.
            return {'type': 'websocket.disconnect', 'code': 1006}
        return self._inbound.pop(0)

    async def send(self, event, *_args, **_kwargs):
        self.sent.append(event)


def _conn(path='/ws') -> Connection:
    return Connection(method='GET', path=path, raw_path=path.encode(),
                      headers=Headers([]), type='websocket')


def _ws(*inbound) -> tuple[WebSocket, _Channel]:
    channel = _Channel(*inbound)
    return WebSocket(_conn(), channel.receive, channel.send), channel


CONNECT = {'type': 'websocket.connect'}


def _text(value):
    return {'type': 'websocket.receive', 'text': value, 'bytes': None}


def _binary(value):
    return {'type': 'websocket.receive', 'text': None, 'bytes': value}


# ---------------------------------------------------------------------------
# Handshake
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_accept_consumes_connect_then_sends_accept():
    ws, channel = _ws(CONNECT)

    await ws.accept()

    assert channel.sent == [{'type': 'websocket.accept', 'subprotocol': None}]
    assert ws.accepted


@pytest.mark.asyncio
async def test_accept_passes_subprotocol_and_headers():
    ws, channel = _ws(CONNECT)

    await ws.accept('chat', headers=[(b'x-trace', b'1')])

    assert channel.sent == [{
        'type': 'websocket.accept',
        'subprotocol': 'chat',
        'headers': [(b'x-trace', b'1')],
    }]


@pytest.mark.asyncio
async def test_accept_defaults_to_none_so_auto_negotiation_still_applies():
    """The actor picks the negotiated subprotocol when the event carries None.

    ``accept()`` with no argument must therefore emit the key *set to None*
    rather than omitting it — omitting it would read the same to ``.get()``,
    but this pins the shape the raw form sends so the two paths stay
    byte-identical on the wire.
    """
    ws, channel = _ws(CONNECT)

    await ws.accept()

    assert 'subprotocol' in channel.sent[0]
    assert channel.sent[0]['subprotocol'] is None


@pytest.mark.asyncio
async def test_accept_twice_is_an_error():
    ws, _ = _ws(CONNECT)
    await ws.accept()

    with pytest.raises(RuntimeError, match='more than once'):
        await ws.accept()


@pytest.mark.asyncio
async def test_accept_raises_when_peer_abandoned_the_handshake():
    ws, channel = _ws({'type': 'websocket.disconnect', 'code': 1001})

    with pytest.raises(WebSocketDisconnect) as excinfo:
        await ws.accept()

    assert excinfo.value.code == 1001
    assert channel.sent == []       # nothing written to a dead transport
    assert ws.client_disconnected


# ---------------------------------------------------------------------------
# Close / reject
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_close_before_accept_rejects_the_handshake():
    ws, channel = _ws(CONNECT)

    await ws.close(4401, 'unauthorized')

    assert channel.sent == [
        {'type': 'websocket.close', 'code': 4401, 'reason': 'unauthorized'}]
    assert not ws.accepted


@pytest.mark.asyncio
async def test_close_after_accept_sends_close():
    ws, channel = _ws(CONNECT)
    await ws.accept()

    await ws.close()

    assert channel.sent[-1] == {'type': 'websocket.close', 'code': 1000}
    assert ws.close_code == 1000


@pytest.mark.asyncio
async def test_close_is_idempotent():
    """``finally: await ws.close()`` must be safe after an explicit close."""
    ws, channel = _ws(CONNECT)
    await ws.accept()

    await ws.close(1000)
    await ws.close(1000)

    assert sum(e['type'] == 'websocket.close' for e in channel.sent) == 1


@pytest.mark.asyncio
async def test_close_after_peer_disconnect_writes_nothing():
    ws, channel = _ws(CONNECT, {'type': 'websocket.disconnect', 'code': 1001})
    await ws.accept()
    with pytest.raises(WebSocketDisconnect):
        await ws.receive()
    channel.sent.clear()

    await ws.close()

    assert channel.sent == []


# ---------------------------------------------------------------------------
# Sending
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_send_text_and_bytes_emit_the_right_key():
    ws, channel = _ws(CONNECT)
    await ws.accept()

    await ws.send_text('hello')
    await ws.send_bytes(b'\x00\x01')

    assert channel.sent[1] == {'type': 'websocket.send', 'text': 'hello'}
    assert channel.sent[2] == {'type': 'websocket.send', 'bytes': b'\x00\x01'}


@pytest.mark.asyncio
async def test_send_json_defaults_to_text():
    ws, channel = _ws(CONNECT)
    await ws.accept()

    await ws.send_json({'ok': True})

    assert json.loads(channel.sent[1]['text']) == {'ok': True}


@pytest.mark.asyncio
async def test_send_json_binary_sends_utf8_bytes():
    ws, channel = _ws(CONNECT)
    await ws.accept()

    await ws.send_json({'ok': True}, binary=True)

    assert json.loads(channel.sent[1]['bytes'].decode()) == {'ok': True}


@pytest.mark.asyncio
async def test_send_dispatches_on_python_type():
    ws, channel = _ws(CONNECT)
    await ws.accept()

    await ws.send('text')
    await ws.send(b'bytes')

    assert 'text' in channel.sent[1]
    assert 'bytes' in channel.sent[2]


@pytest.mark.asyncio
async def test_send_rejects_other_types_with_a_useful_message():
    """Rejected loudly either way; the helpful message is asserted whenever
    beartype did not pre-empt it — which is what an uninstrumented user sees.

    Keyed on the exception actually raised rather than on beartype being
    *importable*: beartype is a test dependency here, so a skipif on the
    import would silently never run this."""
    ws, _ = _ws(CONNECT)
    await ws.accept()

    with pytest.raises(_TYPE_ERRORS) as excinfo:
        await ws.send({'not': 'a frame'})

    intercepted = (_BeartypeViolation is not None
                   and isinstance(excinfo.value, _BeartypeViolation))
    if not intercepted:
        assert 'send_json' in str(excinfo.value)


@pytest.mark.asyncio
async def test_sending_before_accept_is_an_error():
    """A frame before the handshake would be a protocol violation on the wire."""
    ws, channel = _ws(CONNECT)

    with pytest.raises(RuntimeError, match='not accepted'):
        await ws.send_text('too early')

    assert channel.sent == []


@pytest.mark.asyncio
async def test_sending_after_peer_disconnect_raises_disconnect():
    ws, _ = _ws(CONNECT, {'type': 'websocket.disconnect', 'code': 1001})
    await ws.accept()
    with pytest.raises(WebSocketDisconnect):
        await ws.receive()

    with pytest.raises(WebSocketDisconnect):
        await ws.send_text('into the void')


# ---------------------------------------------------------------------------
# Receiving
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_receive_returns_bare_str_and_bytes():
    ws, _ = _ws(CONNECT, _text('hi'), _binary(b'\xff'))
    await ws.accept()

    assert await ws.receive() == 'hi'
    assert await ws.receive() == b'\xff'


@pytest.mark.asyncio
async def test_receive_raises_disconnect_with_code_and_reason():
    ws, _ = _ws(CONNECT,
                {'type': 'websocket.disconnect', 'code': 1001, 'reason': 'bye'})
    await ws.accept()

    with pytest.raises(WebSocketDisconnect) as excinfo:
        await ws.receive()

    assert excinfo.value.code == 1001
    assert excinfo.value.reason == 'bye'
    assert ws.close_code == 1001


@pytest.mark.asyncio
async def test_disconnect_without_a_code_reports_1005():
    """RFC 6455 §7.1.5 — "no status received" is what the app should observe."""
    ws, _ = _ws(CONNECT, {'type': 'websocket.disconnect'})
    await ws.accept()

    with pytest.raises(WebSocketDisconnect) as excinfo:
        await ws.receive()

    assert excinfo.value.code == 1005


@pytest.mark.asyncio
async def test_receive_before_accept_is_an_error():
    ws, _ = _ws(CONNECT, _text('hi'))

    with pytest.raises(RuntimeError, match='not accepted'):
        await ws.receive()


@pytest.mark.asyncio
async def test_typed_receive_helpers():
    ws, _ = _ws(CONNECT, _text('hi'), _binary(b'\x01'), _text('{"a": 1}'))
    await ws.accept()

    assert await ws.receive_text() == 'hi'
    assert await ws.receive_bytes() == b'\x01'
    assert await ws.receive_json() == {'a': 1}


@pytest.mark.asyncio
async def test_receive_text_rejects_a_binary_message():
    ws, _ = _ws(CONNECT, _binary(b'\x01\x02'))
    await ws.accept()

    with pytest.raises(TypeError, match='expected a text message'):
        await ws.receive_text()


@pytest.mark.asyncio
async def test_receive_bytes_rejects_a_text_message():
    ws, _ = _ws(CONNECT, _text('nope'))
    await ws.accept()

    with pytest.raises(TypeError, match='expected a binary message'):
        await ws.receive_bytes()


@pytest.mark.asyncio
async def test_receive_json_accepts_a_binary_message():
    ws, _ = _ws(CONNECT, _binary(b'{"a": 1}'))
    await ws.accept()

    assert await ws.receive_json() == {'a': 1}


# ---------------------------------------------------------------------------
# Iteration
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_async_for_ends_cleanly_at_disconnect():
    """The headline ergonomic: no try/except around the loop."""
    ws, _ = _ws(CONNECT, _text('a'), _text('b'),
                {'type': 'websocket.disconnect', 'code': 1000})
    await ws.accept()

    seen = [message async for message in ws]

    assert seen == ['a', 'b']
    assert ws.client_disconnected
    assert ws.close_code == 1000


@pytest.mark.asyncio
async def test_async_for_yields_text_and_binary_alike():
    ws, _ = _ws(CONNECT, _text('a'), _binary(b'b'),
                {'type': 'websocket.disconnect', 'code': 1000})
    await ws.accept()

    assert [m async for m in ws] == ['a', b'b']


# ---------------------------------------------------------------------------
# Connection facts
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_connection_facts_are_reachable():
    channel = _Channel(CONNECT)
    conn = _conn('/room/42')
    conn.path_params['room'] = '42'
    ws = WebSocket(conn, channel.receive, channel.send)

    assert ws.path == '/room/42'
    assert ws.path_params == {'room': '42'}
    assert ws.connection is conn


@pytest.mark.asyncio
async def test_repr_reports_state():
    ws, _ = _ws(CONNECT)
    assert 'connecting' in repr(ws)
    await ws.accept()
    assert 'open' in repr(ws)
    await ws.close()
    assert 'closed' in repr(ws)


# ---------------------------------------------------------------------------
# Registration — which form the router picks, and when it refuses
# ---------------------------------------------------------------------------

def _registered(app, path):
    return app._router[(path, http.HTTPMethod.GET, Scheme.websocket)]


def test_raw_triplet_handler_is_registered_unwrapped():
    """The compatibility pin: the raw form must reach the router untouched."""
    app = BlackBull()

    @app.route(path='/raw', scheme=Scheme.websocket)
    async def raw(conn, receive, send):
        pass

    assert _registered(app, '/raw') is raw


def test_object_handler_is_wrapped():
    """The registered callable is the ``(conn, receive, send)`` wrapper.

    ``@app.route`` returns the adapted function (as it already does for HTTP),
    so the decorated name *is* the wrapper — ``__wrapped__`` is what
    distinguishes it from the untouched raw form.
    """
    app = BlackBull()

    @app.route(path='/obj', scheme=Scheme.websocket)
    async def obj(ws: WebSocket):
        pass

    registered = _registered(app, '/obj')
    assert registered.__wrapped__.__name__ == 'obj'


def test_raw_triplet_handler_is_not_wrapped_at_all():
    app = BlackBull()

    @app.route(path='/raw2', scheme=Scheme.websocket)
    async def raw(conn, receive, send):
        pass

    assert not hasattr(_registered(app, '/raw2'), '__wrapped__')


def _kinds(plan) -> tuple[str, ...]:
    """Just the categories of a param plan, in signature order."""
    return tuple(kind for _, kind, _ in plan)


@pytest.mark.parametrize('name', ['ws', 'websocket'])
def test_bare_parameter_names_get_the_object(name):
    plan = _websocket_param_plan(eval(f'lambda {name}: None'))
    assert _kinds(plan) == ('ws',)


def test_annotation_wins_over_name():
    """``ws: Connection`` means the Connection, however the parameter is spelt."""
    async def handler(ws: Connection):
        pass

    assert _kinds(_websocket_param_plan(handler)) == ('conn',)


def test_object_and_connection_together():
    async def handler(ws: WebSocket, conn: Connection):
        pass

    assert _kinds(_websocket_param_plan(handler)) == ('ws', 'conn')


def test_unresolvable_parameter_fails_at_registration_not_at_connect_time():
    """Fail fast: an unrecognised parameter must not wait for the first client.

    ``socket`` is the realistic near-miss — a plausible name that is not one
    of the recognised ones, carrying no annotation to disambiguate it.
    """
    app = BlackBull()

    with pytest.raises(TypeError, match='cannot resolve parameter'):
        @app.route(path='/bad', scheme=Scheme.websocket)
        async def bad(socket):   # noqa: F841 — deliberately unrecognised
            pass


def test_an_explicit_annotation_works_under_any_parameter_name():
    """The counterpart to the rule above: annotation beats naming convention."""
    async def handler(sock: WebSocket):
        pass

    assert _kinds(_websocket_param_plan(handler)) == ('ws',)


def test_http_route_is_unaffected_by_the_websocket_branch():
    """An HTTP simplified handler must still get the HTTP adapter."""
    app = BlackBull()

    @app.route(path='/http')
    async def handler(conn):
        return 'ok'

    # Would raise from _websocket_param_plan if the WS branch had claimed it.
    assert app._router[('/http', http.HTTPMethod.GET, Scheme.http)] is not None


def test_dual_scheme_route_keeps_the_http_adapter():
    """Registered for both schemes, the handler still has to satisfy HTTP."""
    app = BlackBull()

    @app.route(path='/both', scheme=[Scheme.http, Scheme.websocket])
    async def handler(conn):
        return 'ok'

    fn = app._router[('/both', http.HTTPMethod.GET, Scheme.websocket)]
    assert fn is not None


@pytest.mark.asyncio
async def test_wrapper_builds_one_object_per_connection():
    """Not per message — an ``async for`` over a long socket must not allocate
    a wrapper per iteration."""
    seen = []

    async def handler(ws: WebSocket):
        seen.append(ws)
        await ws.accept()
        async for _ in ws:
            pass

    adapted = _adapt_websocket_handler(handler)
    channel = _Channel(CONNECT, _text('a'), _text('b'),
                       {'type': 'websocket.disconnect', 'code': 1000})
    await adapted(_conn(), channel.receive, channel.send)

    assert len(seen) == 1


@pytest.mark.asyncio
async def test_wrapper_passes_the_same_object_to_both_parameters():
    captured = {}

    async def handler(ws: WebSocket, conn: Connection):
        captured['ws'] = ws
        captured['conn'] = conn

    adapted = _adapt_websocket_handler(handler)
    conn = _conn()
    channel = _Channel(CONNECT)
    await adapted(conn, channel.receive, channel.send)

    assert captured['conn'] is conn
    assert captured['ws'].connection is conn


@pytest.mark.asyncio
async def test_pre_consumed_handshake_fails_loudly_rather_than_eating_a_message():
    """A third-party middleware that accepts without recording it.

    The built-in `websocket` middleware calls mark_handshake_accepted(), so
    the object adopts the completed handshake.  Anything that accepts
    *without* that marker leaves the client's first message sitting where
    the handshake should be — silently dropping it would be the worst
    outcome, so the object refuses and says what to call.
    """
    ws, _ = _ws(_text('first real message'))

    with pytest.raises(RuntimeError, match='already consumed'):
        await ws.accept()


# ---------------------------------------------------------------------------
# Handshake state shared with middleware
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_object_adopts_a_handshake_completed_by_middleware():
    """No connect event is left to read — the object must not wait for one."""
    from blackbull.websocket import mark_handshake_accepted

    conn = _conn()
    mark_handshake_accepted(conn)
    channel = _Channel(_text('first real message'))
    ws = WebSocket(conn, channel.receive, channel.send)

    assert ws.accepted
    assert await ws.receive() == 'first real message'
    assert channel.sent == []          # no second accept went out


@pytest.mark.asyncio
async def test_bare_accept_under_middleware_is_a_no_op():
    """So the same handler body works with or without the middleware."""
    from blackbull.websocket import mark_handshake_accepted

    conn = _conn()
    mark_handshake_accepted(conn)
    channel = _Channel(_text('hi'))
    ws = WebSocket(conn, channel.receive, channel.send)

    await ws.accept()

    assert channel.sent == []
    assert await ws.receive() == 'hi'


@pytest.mark.asyncio
async def test_accept_with_a_subprotocol_under_middleware_raises():
    """The 101 has gone; silently dropping the request would be worse."""
    from blackbull.websocket import mark_handshake_accepted

    conn = _conn()
    mark_handshake_accepted(conn)
    channel = _Channel()
    ws = WebSocket(conn, channel.receive, channel.send)

    with pytest.raises(RuntimeError, match='already completed by middleware'):
        await ws.accept('chat')


@pytest.mark.asyncio
async def test_a_second_accept_under_middleware_is_still_an_error():
    """The no-op is a one-shot tolerance, not a licence to accept twice."""
    from blackbull.websocket import mark_handshake_accepted

    conn = _conn()
    mark_handshake_accepted(conn)
    channel = _Channel()
    ws = WebSocket(conn, channel.receive, channel.send)

    await ws.accept()
    with pytest.raises(RuntimeError, match='more than once'):
        await ws.accept()


@pytest.mark.asyncio
async def test_close_publishes_state_so_middleware_can_see_it():
    from blackbull.websocket import handshake_closed

    ws, _ = _ws(CONNECT)
    await ws.accept()
    await ws.close(1000)

    assert handshake_closed(ws.connection)


@pytest.mark.asyncio
async def test_accept_publishes_state_so_middleware_can_see_it():
    from blackbull.websocket import handshake_accepted

    ws, _ = _ws(CONNECT)
    await ws.accept()

    assert handshake_accepted(ws.connection)


def test_handshake_helpers_tolerate_a_plain_dict_connection():
    """The middleware's unit tests drive it with a bare {} connection, and the
    BB_FORCE_ASGI_SCOPE boundary threads a scope dict."""
    from blackbull.websocket import (handshake_accepted, handshake_closed,
                                     mark_handshake_accepted,
                                     mark_handshake_closed)

    scope = {}
    assert not handshake_accepted(scope)
    mark_handshake_accepted(scope)
    assert handshake_accepted(scope)

    assert not handshake_closed(scope)
    mark_handshake_closed(scope)
    assert handshake_closed(scope)
