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

import httpx
import pytest

from blackbull import BlackBull
from blackbull.env import reset_settings_cache
from blackbull.testing import NativeTestServer

from ._dual_path_corpus import CLIENT_DRIVABLE


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
    """Four requests, exactly one TCP connection — the actor's keep-alive loop.

    Counted at accept, because that is the only place the answer is a fact.
    ``Connection: close`` is a signal the server *may* send, so its absence
    proves nothing on its own — a server that silently dropped the socket
    between requests would pass a header-only check.
    """
    async with NativeTestServer(app) as server:
        for _ in range(4):
            resp = await server.client.get('/')
            assert resp.status_code == 200
            # Secondary: no response may ask the client to tear down, which is
            # what would have forced a reconnect for the next request.
            assert resp.headers.get('connection', '').lower() != 'close'
        assert server.connections_served == 1


@pytest.mark.asyncio
async def test_the_accept_counter_counts_connections_not_requests(app):
    """Pin the counter's meaning, so the keep-alive proof above can be read.

    Two explicit connections carrying two requests each must read 2, not 4.
    """
    async with NativeTestServer(app) as server:
        for _ in range(2):
            # A fresh client is a fresh pool, hence a fresh TCP connection.
            async with httpx.AsyncClient(base_url=server.url) as client:
                assert (await client.get('/')).status_code == 200
                assert (await client.get('/')).status_code == 200
        assert server.connections_served == 2


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
    done = asyncio.Event()

    @a.on('scope_completed')
    async def _done(event):
        # Append on *every* delivery, signal only the first: a genuine
        # duplicate must still reach ``seen`` and fail the count below.
        seen.append(event)
        done.set()

    @a.route(path='/e')
    async def _e():
        return 'ok'

    async with NativeTestServer(a) as server:
        await server.client.get('/e')
        # Wait for delivery rather than guessing at it — a fixed sleep is a
        # flake on a loaded runner.  The timeout is a safety bound, not a race.
        await asyncio.wait_for(done.wait(), timeout=2.0)
        await asyncio.sleep(0.2)   # bounded settle window for a late duplicate
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


# --- the two lanes agree, over the shared corpus ----------------------------
#
# ``_dual_path_corpus`` is the single definition of "the request shapes the
# compat lane must be invisible for".  ``test_dual_path_identity`` asserts
# byte identity over all of it by driving the actor directly; this asserts the
# same claim one layer out — over a real socket, through a real HTTP client —
# for the subset a conformant client can actually issue.

@pytest.fixture
def corpus_app():
    """The routes the shared corpus addresses (mirrors the identity test's)."""
    a = BlackBull()

    @a.route(path='/')
    async def _root():
        return 'hello'

    @a.route(path='/echo', methods=['POST'])
    async def _echo(conn, receive, send):
        body = await conn.body()
        await send(body or b'(empty)', HTTPStatus.OK)

    @a.route(path='/café')
    async def _unicode_path():
        return 'cafe'

    return a


#: Headers that may legitimately differ between two runs of the same request.
_VOLATILE_HEADERS = frozenset({'date'})


def _observable(resp):
    """What a client sees, minus what is allowed to vary between runs."""
    return (
        resp.status_code,
        sorted((k.lower(), v) for k, v in resp.headers.multi_items()
               if k.lower() not in _VOLATILE_HEADERS),
        resp.content,
    )


async def _drive_through_server(app, spec):
    async with NativeTestServer(app) as server:
        return _observable(await server.client.request(
            spec.method, spec.target,
            headers=list(spec.headers) or None,
            content=spec.body or None,
        ))


@pytest.mark.asyncio
@pytest.mark.parametrize('name', sorted(CLIENT_DRIVABLE))
async def test_both_lanes_agree_over_the_shared_corpus(corpus_app, name,
                                                       monkeypatch):
    """Native and ``BB_FORCE_ASGI_SCOPE=1`` agree on what a client observes.

    The vectors come from ``_dual_path_corpus.CLIENT_DRIVABLE`` — the same
    definition ``test_dual_path_identity`` draws from — so the claim is stated
    once and asserted at two depths, rather than re-specified here.
    """
    spec = CLIENT_DRIVABLE[name]

    reset_settings_cache()
    native = await _drive_through_server(corpus_app, spec)

    monkeypatch.setenv('BB_FORCE_ASGI_SCOPE', '1')
    reset_settings_cache()
    try:
        forced = await _drive_through_server(corpus_app, spec)
    finally:
        monkeypatch.delenv('BB_FORCE_ASGI_SCOPE', raising=False)
        reset_settings_cache()

    assert forced == native, (
        f'{name}: the compat lane diverged from the native lane over a real '
        f'socket.\n  native: {native}\n  forced: {forced}')
