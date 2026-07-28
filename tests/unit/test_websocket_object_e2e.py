"""The :class:`~blackbull.WebSocket` object over the real stack (Sprint 82).

``test_websocket_object.py`` pins the wrapper against a scripted channel.
This file drives it through :class:`~blackbull.testing.TestClient` — the
router, the WebSocket actor, the recipient, the codec, and the sender — so
the object's events are proven to be the ones the actor actually accepts.

The load-bearing pair is :func:`test_object_and_raw_forms_are_indistinguishable_on_the_wire`
and its neighbours: the whole premise of the sprint is that this is a
*handler-side* convenience with no protocol consequence.
"""
import pytest

from blackbull import BlackBull, WebSocket
from blackbull.testing import TestClient, WebSocketDisconnect
from blackbull.utils import Scheme


def _make_app() -> BlackBull:
    app = BlackBull()

    @app.route(path='/object-echo', scheme=Scheme.websocket)
    async def object_echo(ws: WebSocket):
        await ws.accept()
        async for message in ws:
            await ws.send(message)

    @app.route(path='/raw-echo', scheme=Scheme.websocket)
    async def raw_echo(conn, receive, send):
        await receive()
        await send({'type': 'websocket.accept'})
        while True:
            event = await receive()
            if event.get('type') == 'websocket.disconnect':
                return
            if event.get('type') == 'websocket.receive':
                if event.get('text') is not None:
                    await send({'type': 'websocket.send', 'text': event['text']})
                elif event.get('bytes') is not None:
                    await send({'type': 'websocket.send', 'bytes': event['bytes']})

    @app.route(path='/object-reject', scheme=Scheme.websocket)
    async def object_reject(ws: WebSocket):
        await ws.close(4401, 'nope')

    @app.route(path='/object-stream', scheme=Scheme.websocket)
    async def object_stream(ws: WebSocket):
        await ws.accept()
        for i in range(3):
            await ws.send_text(f'msg-{i}')
        await ws.close()

    @app.route(path='/object-json', scheme=Scheme.websocket)
    async def object_json(ws: WebSocket):
        await ws.accept()
        payload = await ws.receive_json()
        await ws.send_json({'echo': payload})
        await ws.close()

    @app.route(path='/object-subprotocol', scheme=Scheme.websocket)
    async def object_subprotocol(ws: WebSocket):
        offered = ws.subprotocols
        await ws.accept(offered[0] if offered else None)
        await ws.send_text(str(ws.subprotocols))
        await ws.close()

    @app.route(path='/object-bare', scheme=Scheme.websocket)
    async def object_bare(ws):
        """The un-annotated form — resolved by parameter name."""
        await ws.accept()
        await ws.send_text('bare')
        await ws.close()

    @app.route(path='/object-with-conn', scheme=Scheme.websocket)
    async def object_with_conn(ws: WebSocket, conn):
        await ws.accept()
        await ws.send_text(conn.path)
        await ws.close()

    @app.route(path='/disconnect-observed', scheme=Scheme.websocket)
    async def disconnect_observed(ws: WebSocket):
        await ws.accept()
        async for _ in ws:
            pass
        # The loop ended because the peer went away — record that we saw it.
        app.state_seen = (ws.client_disconnected, ws.close_code)

    return app


# ---------------------------------------------------------------------------
# Equivalence with the raw form
# ---------------------------------------------------------------------------

@pytest.mark.parametrize('path', ['/object-echo', '/raw-echo'])
def test_object_and_raw_forms_are_indistinguishable_on_the_wire(path):
    """The sprint's core claim, tested as one parametrisation over both forms.

    If the wrapper ever changed framing, fragmentation, or close semantics,
    the two parametrisations would diverge here.
    """
    app = _make_app()
    with TestClient(app) as client:
        with client.websocket_connect(path) as ws:
            ws.send_text('hello')
            assert ws.receive_text() == 'hello'
            ws.send_bytes(b'\x00\xff')
            assert ws.receive_bytes() == b'\x00\xff'


# ---------------------------------------------------------------------------
# The object form end to end
# ---------------------------------------------------------------------------

def test_object_echo_round_trips_text():
    app = _make_app()
    with TestClient(app) as client:
        with client.websocket_connect('/object-echo') as ws:
            ws.send_text('ping')
            assert ws.receive_text() == 'ping'


def test_object_reject_closes_with_the_given_code():
    app = _make_app()
    with TestClient(app) as client:
        with pytest.raises(WebSocketDisconnect) as excinfo:
            with client.websocket_connect('/object-reject'):
                pass
    assert excinfo.value.code == 4401


def test_object_stream_then_close():
    app = _make_app()
    with TestClient(app) as client:
        with client.websocket_connect('/object-stream') as ws:
            assert list(ws.iter_text()) == ['msg-0', 'msg-1', 'msg-2']


def test_send_json_and_receive_json():
    app = _make_app()
    with TestClient(app) as client:
        with client.websocket_connect('/object-json') as ws:
            ws.send_text('{"a": 1}')
            assert ws.receive_json() == {'echo': {'a': 1}}


def test_subprotocol_negotiation_through_the_object():
    app = _make_app()
    with TestClient(app) as client:
        with client.websocket_connect('/object-subprotocol',
                                      subprotocols=['chat', 'json']) as ws:
            assert 'chat' in ws.receive_text()


def test_bare_parameter_name_resolves_to_the_object():
    app = _make_app()
    with TestClient(app) as client:
        with client.websocket_connect('/object-bare') as ws:
            assert ws.receive_text() == 'bare'


def test_connection_can_be_injected_alongside_the_object():
    app = _make_app()
    with TestClient(app) as client:
        with client.websocket_connect('/object-with-conn') as ws:
            assert ws.receive_text() == '/object-with-conn'


def test_client_disconnect_ends_the_async_for_and_is_observable():
    app = _make_app()
    with TestClient(app) as client:
        with client.websocket_connect('/disconnect-observed') as ws:
            ws.send_text('one')
        # Leaving the context closes from the client side.
    disconnected, code = app.state_seen
    assert disconnected
    assert code is not None


# ---------------------------------------------------------------------------
# Composition with the handshake middleware
# ---------------------------------------------------------------------------
#
# `blackbull.middleware.websocket` accepts before the handler runs.  It
# records that on the connection, so a WebSocket object built downstream
# adopts the completed handshake rather than reading the client's first
# message and mistaking it for one.

def _middleware_app() -> BlackBull:
    from blackbull.middleware import websocket as ws_middleware

    app = BlackBull()

    @app.route(path='/mw-object', scheme=Scheme.websocket,
               middlewares=[ws_middleware])
    async def mw_object(ws: WebSocket):
        """No accept() at all — the middleware did it."""
        async for message in ws:
            await ws.send(message)

    @app.route(path='/mw-object-accepts', scheme=Scheme.websocket,
               middlewares=[ws_middleware])
    async def mw_object_accepts(ws: WebSocket):
        """A bare accept() is tolerated, so the body is form-agnostic."""
        await ws.accept()
        async for message in ws:
            await ws.send(message)

    @app.route(path='/mw-object-closes', scheme=Scheme.websocket,
               middlewares=[ws_middleware])
    async def mw_object_closes(ws: WebSocket):
        await ws.send_text('bye')
        await ws.close(1000)

    @app.route(path='/mw-raw', scheme=Scheme.websocket,
               middlewares=[ws_middleware])
    async def mw_raw(conn, receive, send):
        """The form the middleware was written for — unchanged."""
        while True:
            event = await receive()
            if event.get('type') == 'websocket.disconnect':
                return
            if event.get('type') == 'websocket.receive':
                await send({'type': 'websocket.send',
                            'text': event.get('text') or ''})

    return app


def test_middleware_plus_object_no_accept_in_handler():
    """The regression this whole mechanism exists for.

    Before the handshake was published on the connection, the object would
    read the client's first message as its `websocket.connect` — so 'hello'
    was silently swallowed and the echo never arrived.
    """
    app = _middleware_app()
    with TestClient(app) as client:
        with client.websocket_connect('/mw-object') as ws:
            ws.send_text('hello')
            assert ws.receive_text() == 'hello'


def test_middleware_plus_object_with_a_bare_accept():
    app = _middleware_app()
    with TestClient(app) as client:
        with client.websocket_connect('/mw-object-accepts') as ws:
            ws.send_text('hello')
            assert ws.receive_text() == 'hello'


def test_middleware_still_works_with_the_raw_form():
    app = _middleware_app()
    with TestClient(app) as client:
        with client.websocket_connect('/mw-raw') as ws:
            ws.send_text('hello')
            assert ws.receive_text() == 'hello'


def test_handler_close_suppresses_the_middlewares_trailing_close():
    """Only one close frame — the handler's."""
    app = _middleware_app()
    with TestClient(app) as client:
        with client.websocket_connect('/mw-object-closes') as ws:
            assert ws.receive_text() == 'bye'
            with pytest.raises(WebSocketDisconnect) as excinfo:
                ws.receive_text()
    assert excinfo.value.code == 1000
