"""Tier 1 of the native test client — ``blackbull.testing.native``.

The instrument under test injects a typed :class:`Connection` and calls
``app(conn, receive, send)`` directly, which is the entry point BlackBull's
own protocol actors use.  ``TestClient`` cannot reach that entry point: it
drives the app through ``httpx.ASGITransport`` → ASGI scope dict →
``from_scope()``, so the ``isinstance(conn, Connection)`` branch of
``BlackBull.__call__`` — the production hot path — is never taken.

These tests therefore assert two things at once: that the helpers behave like
a request client, and that the object the handler receives is the native
``Connection`` rather than a rebuilt one.
"""
import asyncio
from http import HTTPStatus

import pytest

from blackbull import BlackBull
from blackbull.connection import Connection
from blackbull.headers import Headers
from blackbull.testing import native
from blackbull.testing.native import NativeClient, NativeResponse


@pytest.fixture
def app():
    a = BlackBull()

    @a.route(path='/')
    async def _root():
        return 'hello'

    @a.route(path='/json')
    async def _json():
        return {'ok': True, 'n': 3}

    @a.route(path='/echo', methods=['POST'])
    async def _echo(conn, receive, send):
        await send(await conn.body(), HTTPStatus.OK)

    @a.route(path='/who')
    async def _who(conn, receive, send):
        # The native branch hands the handler the very object the caller
        # built; the compat branch would hand it a from_scope() rebuild.
        await send(type(conn).__name__.encode(), HTTPStatus.OK)

    @a.route(path='/inspect')
    async def _inspect(conn, receive, send):
        body = (f'{conn.method}|{conn.path}|{conn.query_string.decode()}|'
                f'{conn.http_version}|{conn.scheme}').encode()
        await send(body, HTTPStatus.OK)

    @a.route(path='/hdr')
    async def _hdr(conn, receive, send):
        await send(conn.headers.get(b'x-probe', b'<none>'), HTTPStatus.OK)

    @a.route(path='/items/{item_id}')
    async def _item(item_id):
        return f'item={item_id}'

    @a.route(path='/café')
    async def _unicode():
        return 'cafe'

    return a


# --- the core contract ------------------------------------------------------

@pytest.mark.asyncio
async def test_get_returns_a_native_response(app):
    resp = await native.get(app, '/')
    assert isinstance(resp, NativeResponse)
    assert resp.status == 200
    assert resp.body == b'hello'


@pytest.mark.asyncio
async def test_handler_receives_the_native_connection(app):
    """The whole point of Tier 1: ``BlackBull.__call__``'s native branch.

    Under ``TestClient`` this handler would still see a ``Connection``, but
    a *different* one — rebuilt by ``from_scope()``.  Asserting identity is
    what distinguishes the two lanes.
    """
    conn = Connection(method='GET', path='/who', raw_path=b'/who',
                      headers=Headers([(b'host', b'testserver')]))
    resp = await native.request(app, conn)
    assert resp.body == b'Connection'


@pytest.mark.asyncio
async def test_request_threads_the_caller_s_connection_object(app):
    """No conversion happens: the handler's ``conn`` *is* the injected one."""
    seen = []

    a = BlackBull()

    @a.route(path='/id')
    async def _id(conn, receive, send):
        seen.append(conn)
        await send(b'ok', HTTPStatus.OK)

    conn = Connection(method='GET', path='/id', raw_path=b'/id',
                      headers=Headers([(b'host', b'testserver')]))
    await native.request(a, conn)
    assert seen and seen[0] is conn


@pytest.mark.asyncio
async def test_response_headers_are_a_headers_object(app):
    resp = await native.get(app, '/')
    assert isinstance(resp.headers, Headers)
    assert resp.headers.get(b'content-type') == b'text/html; charset=utf-8'


@pytest.mark.asyncio
async def test_framing_headers_are_a_tier_2_observation(app):
    """Tier 1 reports the events the *app* emitted, not the bytes sent.

    ``Content-Length`` is injected by the protocol sender, below this tier, so
    its absence here is the boundary working as designed — and the reason a
    test about framing belongs on :class:`NativeTestServer`.
    """
    resp = await native.get(app, '/')
    assert resp.headers.getlist(b'content-length') == []


@pytest.mark.asyncio
async def test_json_and_text_helpers(app):
    resp = await native.get(app, '/json')
    assert resp.json() == {'ok': True, 'n': 3}
    assert resp.text() == '{"ok": true, "n": 3}'


# --- request construction ---------------------------------------------------

@pytest.mark.asyncio
async def test_query_string_is_split_from_the_path(app):
    resp = await native.get(app, '/inspect?a=1&b=2')
    method, path, query, version, scheme = resp.text().split('|')
    assert (method, path, query) == ('GET', '/inspect', 'a=1&b=2')
    assert (version, scheme) == ('1.1', 'http')


@pytest.mark.asyncio
async def test_percent_encoded_path_is_decoded_like_the_parser(app):
    """``conn.path`` is decoded, ``raw_path`` is not — the H/1.1 parser's rule."""
    resp = await native.get(app, '/caf%C3%A9')
    assert resp.status == 200
    assert resp.body == b'cafe'


@pytest.mark.asyncio
async def test_path_params_are_matched(app):
    resp = await native.get(app, '/items/42')
    assert resp.body == b'item=42'


@pytest.mark.asyncio
async def test_headers_reach_the_handler(app):
    resp = await native.get(app, '/hdr', headers={b'x-probe': b'seen'})
    assert resp.body == b'seen'


@pytest.mark.asyncio
async def test_headers_accept_str_pairs(app):
    resp = await native.get(app, '/hdr', headers={'X-Probe': 'STR'})
    assert resp.body == b'STR'


@pytest.mark.asyncio
async def test_a_host_header_is_supplied_by_default(app):
    a = BlackBull()

    @a.route(path='/host')
    async def _host(conn, receive, send):
        await send(conn.headers.get(b'host', b'<none>'), HTTPStatus.OK)

    resp = await native.get(a, '/host')
    assert resp.body != b'<none>'


# --- bodies -----------------------------------------------------------------

@pytest.mark.asyncio
async def test_post_body_reaches_conn_body(app):
    resp = await native.post(app, '/echo', body=b'payload')
    assert resp.body == b'payload'


@pytest.mark.asyncio
async def test_body_implies_content_length(app):
    """A real request carries the framing header; a synthesised one must too,
    or middleware that branches on ``content-length`` behaves differently
    under test than on the wire."""
    a = BlackBull()

    @a.route(path='/len', methods=['POST'])
    async def _len(conn, receive, send):
        await send(conn.headers.get(b'content-length', b'<none>'), HTTPStatus.OK)

    resp = await native.post(a, '/len', body=b'12345')
    assert resp.body == b'5'


@pytest.mark.asyncio
async def test_explicit_content_length_is_not_overwritten():
    a = BlackBull()

    @a.route(path='/len', methods=['POST'])
    async def _len(conn, receive, send):
        await send(conn.headers.get(b'content-length'), HTTPStatus.OK)

    resp = await native.post(a, '/len', body=b'12345',
                             headers={b'content-length': b'99'})
    assert resp.body == b'99'


@pytest.mark.asyncio
async def test_str_body_is_utf8_encoded():
    a = BlackBull()

    @a.route(path='/echo', methods=['POST'])
    async def _echo(conn, receive, send):
        await send(await conn.body(), HTTPStatus.OK)

    resp = await native.post(a, '/echo', body='café')
    assert resp.body == 'café'.encode('utf-8')


@pytest.mark.asyncio
async def test_json_argument_sets_body_and_content_type():
    a = BlackBull()

    @a.route(path='/j', methods=['POST'])
    async def _j(conn, receive, send):
        payload = await conn.json()
        ct = conn.headers.get(b'content-type', b'')
        await send(f'{payload["k"]}|{ct.decode()}'.encode(), HTTPStatus.OK)

    resp = await native.post(a, '/j', json={'k': 'v'})
    assert resp.body == b'v|application/json'


@pytest.mark.asyncio
async def test_streaming_response_is_reassembled():
    a = BlackBull()

    @a.route(path='/stream')
    async def _stream(conn, receive, send):
        await send({'type': 'http.response.start', 'status': 200, 'headers': []})
        for chunk in (b'a', b'b', b'c'):
            await send({'type': 'http.response.body', 'body': chunk, 'more_body': True})
        await send({'type': 'http.response.body', 'body': b'', 'more_body': False})

    resp = await native.get(a, '/stream')
    assert resp.body == b'abc'
    assert resp.status == 200


# --- verbs ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_every_verb_helper_exists(app):
    a = BlackBull()

    @a.route(path='/any', methods=['GET', 'POST', 'PUT', 'PATCH', 'DELETE',
                                   'HEAD', 'OPTIONS'])
    async def _any(conn, receive, send):
        await send(conn.method.encode(), HTTPStatus.OK)

    assert (await native.get(a, '/any')).body == b'GET'
    assert (await native.post(a, '/any')).body == b'POST'
    assert (await native.put(a, '/any')).body == b'PUT'
    assert (await native.patch(a, '/any')).body == b'PATCH'
    assert (await native.delete(a, '/any')).body == b'DELETE'
    assert (await native.options(a, '/any')).body == b'OPTIONS'
    # HEAD reaches the handler as HEAD here: the GET-rewrite is the H/1.1
    # actor's job (RFC 9110 §9.3.2), and Tier 1 starts below the actor.
    assert (await native.head(a, '/any')).status == 200


# --- misses and rejections --------------------------------------------------

@pytest.mark.asyncio
async def test_unrouted_path_is_404(app):
    resp = await native.get(app, '/nope')
    assert resp.status == 404


@pytest.mark.asyncio
async def test_wrong_method_is_405(app):
    resp = await native.delete(app, '/')
    assert resp.status == 405


# --- integration with the rest of the framework -----------------------------

@pytest.mark.asyncio
async def test_middleware_runs():
    a = BlackBull()

    async def _mw(conn, receive, send, call_next):
        conn.state['tag'] = 'mw'
        await call_next(conn, receive, send)

    a.use(_mw)

    @a.route(path='/m')
    async def _m(conn, receive, send):
        await send(conn.state.get('tag', '<none>').encode(), HTTPStatus.OK)

    resp = await native.get(a, '/m')
    assert resp.body == b'mw'


@pytest.mark.asyncio
async def test_lifecycle_events_fire_exactly_once():
    a = BlackBull()
    seen = []

    @a.on('scope_completed')
    async def _done(event):
        seen.append(event)

    @a.route(path='/e')
    async def _e():
        return 'ok'

    await native.get(a, '/e')
    await asyncio.sleep(0.05)   # settle window for a late duplicate
    assert len(seen) == 1


@pytest.mark.asyncio
async def test_handler_exception_becomes_500():
    a = BlackBull()

    @a.route(path='/boom')
    async def _boom():
        raise RuntimeError('kaboom')

    resp = await native.get(a, '/boom')
    assert resp.status == 500


# --- the sync façade --------------------------------------------------------

def test_native_client_is_a_sync_facade(app):
    with NativeClient(app) as client:
        resp = client.get('/')
        assert resp.status == 200
        assert resp.body == b'hello'


def test_native_client_serves_many_requests_on_one_loop(app):
    with NativeClient(app) as client:
        assert client.get('/').body == b'hello'
        assert client.post('/echo', body=b'x').body == b'x'
        assert client.get('/nope').status == 404


def test_native_client_outside_context_manager_is_an_error(app):
    client = NativeClient(app)
    with pytest.raises(RuntimeError, match='context manager'):
        client.get('/')


def test_native_client_runs_lifespan(app):
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

    with NativeClient(a) as client:
        client.get('/x')
        assert order == ['up']
    assert order == ['up', 'down']
