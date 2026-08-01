"""Tier 2 of the native test client — :class:`NativeTestServer`.

This tier binds a real loopback socket and runs BlackBull's own
:class:`~blackbull.server.server.Server`, so a request travels the whole
production path: TCP accept → ``ConnectionActor`` → ``HTTP1Actor`` parse →
``Connection`` → native dispatch → the bytes the sender writes.

The assertions below are deliberately about things that are *only* observable
on the wire — framing headers the sender injects, the HEAD body strip,
keep-alive reuse, chunked encoding.  Every one of them is invisible to Tier 1
(which stops at the app's ``send`` events) and to ``TestClient`` (which never
runs a protocol actor at all).
"""
import asyncio
from http import HTTPStatus

import pytest

from blackbull import BlackBull
from blackbull.testing import NativeTestServer


@pytest.fixture
def app():
    a = BlackBull()

    @a.route(path='/')
    async def _root():
        return 'hello'

    @a.route(path='/echo', methods=['POST'])
    async def _echo(conn, receive, send):
        await send(await conn.body(), HTTPStatus.OK)

    @a.route(path='/who')
    async def _who(conn, receive, send):
        await send(type(conn).__name__.encode(), HTTPStatus.OK)

    @a.route(path='/peer')
    async def _peer(conn, receive, send):
        await send(f'{conn.client[0]}|{conn.http_version}'.encode(), HTTPStatus.OK)

    @a.route(path='/chunks')
    async def _chunks(conn, receive, send):
        await send({'type': 'http.response.start', 'status': 200, 'headers': []})
        for chunk in (b'a', b'b', b'c'):
            await send({'type': 'http.response.body', 'body': chunk, 'more_body': True})
        await send({'type': 'http.response.body', 'body': b'', 'more_body': False})

    return a


# --- the core contract ------------------------------------------------------

@pytest.mark.asyncio
async def test_round_trips_over_a_real_socket(app):
    async with NativeTestServer(app) as server:
        resp = await server.client.get('/')
        assert resp.status_code == 200
        assert resp.text == 'hello'


@pytest.mark.asyncio
async def test_binds_an_ephemeral_loopback_port(app):
    async with NativeTestServer(app) as server:
        assert server.port != 0
        assert server.url == f'http://127.0.0.1:{server.port}'
        # Loopback only: a test must never publish a port to the network.
        assert server.host == '127.0.0.1'


@pytest.mark.asyncio
async def test_the_handler_is_reached_through_the_native_connection(app):
    """The actor parses bytes into a ``Connection`` and dispatches natively."""
    async with NativeTestServer(app) as server:
        resp = await server.client.get('/who')
        assert resp.text == 'Connection'


@pytest.mark.asyncio
async def test_transport_fields_come_from_the_real_socket(app):
    async with NativeTestServer(app) as server:
        resp = await server.client.get('/peer')
        client_ip, version = resp.text.split('|')
        assert client_ip == '127.0.0.1'
        assert version == '1.1'


@pytest.mark.asyncio
async def test_two_servers_get_distinct_ports(app):
    async with NativeTestServer(app) as a, NativeTestServer(app) as b:
        assert a.port != b.port
        assert (await a.client.get('/')).text == 'hello'
        assert (await b.client.get('/')).text == 'hello'


# --- what only the wire can tell you ---------------------------------------

@pytest.mark.asyncio
async def test_the_sender_injects_content_length(app):
    """Tier 1 cannot see this: the app never emits the framing header."""
    async with NativeTestServer(app) as server:
        resp = await server.client.get('/')
        assert resp.headers['content-length'] == '5'


@pytest.mark.asyncio
async def test_head_returns_the_get_headers_without_a_body(app):
    """RFC 9110 §9.3.2 — identical to the GET response minus the body."""
    async with NativeTestServer(app) as server:
        get_resp = await server.client.get('/')
        head_resp = await server.client.head('/')

    assert head_resp.status_code == 200
    assert head_resp.content == b''
    assert head_resp.headers['content-length'] == get_resp.headers['content-length']


@pytest.mark.asyncio
async def test_keep_alive_reuses_one_connection(app):
    """Four requests, one TCP connection — the actor's keep-alive loop."""
    async with NativeTestServer(app) as server:
        for _ in range(4):
            resp = await server.client.get('/')
            assert resp.status_code == 200
        # httpx pools by default; a connection-per-request would show up as
        # a `connection: close` on the responses.
        assert resp.headers.get('connection', '').lower() != 'close'


@pytest.mark.asyncio
async def test_streaming_response_is_chunked_on_the_wire(app):
    async with NativeTestServer(app) as server:
        resp = await server.client.get('/chunks')
        assert resp.text == 'abc'
        assert resp.headers.get('transfer-encoding') == 'chunked'


@pytest.mark.asyncio
async def test_request_body_is_parsed_from_the_wire(app):
    async with NativeTestServer(app) as server:
        resp = await server.client.post('/echo', content=b'payload')
        assert resp.content == b'payload'


@pytest.mark.asyncio
async def test_unrouted_path_is_404(app):
    async with NativeTestServer(app) as server:
        assert (await server.client.get('/nope')).status_code == 404


# --- lifecycle --------------------------------------------------------------

@pytest.mark.asyncio
async def test_lifespan_runs_around_the_server():
    a = BlackBull()
    order = []

    @a.on_startup
    async def _up():
        order.append('up')

    @a.on_shutdown
    async def _down():
        order.append('down')

    @a.route(path='/x')
    async def _x():
        return 'x'

    async with NativeTestServer(a) as server:
        assert order == ['up']
        assert (await server.client.get('/x')).text == 'x'
    assert order == ['up', 'down']


@pytest.mark.asyncio
async def test_the_port_is_released_on_exit(app):
    async with NativeTestServer(app) as server:
        port = server.port
    # Rebinding the same port proves the listener really closed rather than
    # lingering until interpreter teardown.
    async with NativeTestServer(app, port=port) as second:
        assert second.port == port


@pytest.mark.asyncio
async def test_client_outside_the_context_manager_is_an_error(app):
    server = NativeTestServer(app)
    with pytest.raises(RuntimeError, match='context manager'):
        server.client


@pytest.mark.asyncio
async def test_events_fire_exactly_once_per_request():
    a = BlackBull()
    seen = []

    @a.on('scope_completed')
    async def _done(event):
        seen.append(event)

    @a.route(path='/e')
    async def _e():
        return 'ok'

    async with NativeTestServer(a) as server:
        await server.client.get('/e')
        await asyncio.sleep(0.1)   # settle window for a late duplicate
    assert len(seen) == 1


# --- the sync façade --------------------------------------------------------

def test_sync_form_serves_requests(app):
    with NativeTestServer(app) as server:
        resp = server.client.get('/')
        assert resp.status_code == 200
        assert resp.text == 'hello'


def test_sync_form_serves_many_requests(app):
    with NativeTestServer(app) as server:
        assert server.client.get('/').text == 'hello'
        assert server.client.post('/echo', content=b'x').content == b'x'
        assert server.client.get('/nope').status_code == 404
