from http import HTTPStatus, HTTPMethod
from unittest.mock import AsyncMock
import ssl
import pathlib

import logging

# Library for test-fixture
from multiprocessing import Process
import asyncio
import pytest
import pytest_asyncio

# Test targets
from blackbull import BlackBull, Response, WebSocketResponse
from blackbull.utils import Scheme
# from blackbull.middlewares import websocket

# Library for tests
import httpx
import websockets

logger = logging.getLogger(__name__)


def run_application(server):
    """Subprocess entry point — drive a pre-bound ASGIServer to completion."""
    logger.info('run_application is called.')
    asyncio.run(server.run())


async def wait_for_server(host, port, *, timeout=10.0, interval=0.1):
    """Poll host:port until a TCP connection succeeds or *timeout* seconds elapse.

    The server runs in a child process, so there is an inherent race between
    the fixture yielding and the server actually accepting connections.
    Without this wait the test connects before the server is ready, causing
    a ConnectionRefusedError (or a TimeoutError if websockets retries silently).
    """
    import socket as _socket
    deadline = asyncio.get_event_loop().time() + timeout
    while True:
        try:
            _, writer = await asyncio.open_connection(host, port)
            writer.close()
            await writer.wait_closed()
            return  # server is up
        except (ConnectionRefusedError, OSError):
            if asyncio.get_event_loop().time() >= deadline:
                raise TimeoutError(f'Server on {host}:{port} did not become ready within {timeout}s')
            await asyncio.sleep(interval)


@pytest_asyncio.fixture
async def app(manage_cert_and_key):
    logger.info('At set-up.')
    cd = pathlib.Path(__file__).parent.parent
    cert_path = cd / 'cert.pem'
    key_path = cd / 'key.pem'

    app = BlackBull()
    from blackbull.server import ASGIServer

    logger.info(f"[app fixture] cert.pem exists: {cert_path.exists()}, key.pem exists: {key_path.exists()}")

    server = ASGIServer(app, certfile=str(cert_path), keyfile=str(key_path))
    server.open_socket(0)

    # Routing not using middleware.
    @app.route(path='/test')
    async def test_(scope, receive, send):
        logger.debug(f'test_({scope}, {receive}, {send})')
        await send(Response('sample'))

    @app.on_error(HTTPStatus.NOT_FOUND)
    async def test_404(scope, receive, send):
        logger.debug(f'test_404({scope}, {receive}, {send})')
        await send(Response('not found test.', status=HTTPStatus.NOT_FOUND))

    # Routing using middleware.
    async def test_fn1(scope, receive, send, inner):
        logger.debug('test_fn1 starts.')
        res = await inner(scope, receive, send)
        logger.debug(f'test_fn1 ends. res = {res}')
        await send(Response(res + 'fn1'))

    async def test_fn2(scope, receive, send, inner):
        logger.debug('test_fn2 starts.')
        res = await inner(scope, receive, send)
        logger.debug(f'test_fn2 ends. res = {res}')
        return res + 'fn2'

    async def test_fn3(scope, receive, send, inner):
        logger.debug('test_fn3 starts.')
        await inner(scope, receive, send)
        logger.debug('test_fn3 ends.')
        return 'fn3'

    app.route(methods=HTTPMethod.GET, path='/test2', functions=[test_fn1, test_fn2, test_fn3])

    @app.route(path='/websocket1', scheme=Scheme.websocket)
    async def websocket1(scope, receive, send):
        accept = {"type": "websocket.accept", "subprotocol": None}
        msg = await receive()  # consume websocket.connect
        await send(accept)

        while msg := (await receive()):
            if msg.get('type') == 'websocket.disconnect':
                break
            if 'text' in msg and msg['text'] is not None:
                logger.debug(f'Got a text massage ({msg}.)')
                await send(WebSocketResponse(msg['text']))
            elif 'bytes' in msg and msg['bytes'] is not None:
                logger.debug(f'Got a byte-string massage ({msg}.)')
                await send(WebSocketResponse(msg['bytes']))
            else:
                logger.info('The received message does not contain any message.')
                break

        await send({'type': 'websocket.close'})

    async def websocket2(scope, receive, send):
        while msg := (await receive()):
            await send(WebSocketResponse(msg))

    app.route(path='/websocket2', scheme=Scheme.websocket,
              functions=[websocket2])

    @app.route(path='/push', methods=[HTTPMethod.POST])
    async def server_push(scope, receive, send):
        # await Response(send, 'Any message?', more_body=True)
        request = await receive()

        while request['type'] != 'http.disconnect' and request['body'] != 'Bye':
            msg = request['body']
            await send(Response(msg))

            try:
                request = await asyncio.wait_for(receive(), timeout=0.5)

            except asyncio.TimeoutError:
                logger.debug('Have not received any message in this second.')
                await send(Response('Any message?'))

    p = Process(target=run_application, args=(server,))
    p.start()
    server.wait_for_port(timeout=10.0)

    # Wait until the server is actually accepting connections before yielding
    # to the test.  Without this, the test races against server startup and
    # gets a ConnectionRefusedError or a silent TimeoutError.
    await wait_for_server('127.0.0.1', server.port)

    # Tests dereference `app.port` for URL construction; expose it as a
    # transient attribute on the BlackBull instance just for this fixture.
    app.port = server.port

    try:
        yield app
    finally:
        logger.info('At teardown.')
        server.close()
        p.terminate()
        p.join(timeout=5)


@pytest_asyncio.fixture
async def ssl_context():
    ssl_context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    logger.info(pathlib.Path(__file__))
    localhost_pem = pathlib.Path(__file__).parent.parent / "cert.pem"
    ssl_context.load_verify_locations(localhost_pem)

    yield ssl_context

    # At tear down.
    pass


@pytest_asyncio.fixture
async def ssl_h2context():
    ssl_context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    logger.info(pathlib.Path(__file__))
    localhost_pem = pathlib.Path(__file__).parent.parent / "cert.pem"
    ssl_context.load_verify_locations(localhost_pem)
    ssl_context.set_alpn_protocols(['h2'])

    yield ssl_context

    # At tear down.
    pass


def _validating_ssl_context() -> ssl.SSLContext:
    """SSLContext that validates the chain + hostname against the self-signed
    test cert, instead of ``verify=False``.

    The test cert carries an ``IP:127.0.0.1`` SAN, so connecting to the IPv4
    loopback validates cleanly.  Using a real context (rather than disabling
    verification) keeps CodeQL from flagging the call as "Request without
    certificate validation" — the same rationale as
    ``test_http2_advanced._test_ssl_context``.
    """
    localhost_pem = pathlib.Path(__file__).parent.parent / 'cert.pem'
    return ssl.create_default_context(cafile=str(localhost_pem))


# These response-code tests connect to the literal IPv4 loopback rather than
# the name 'localhost'.  On GitHub-hosted runners 'localhost' resolves to the
# IPv6 loopback '::1' first, and that path drops the TLS connection mid-handshake
# ("httpx.RemoteProtocolError: Server disconnected") even though the dual-stack
# listener binds '::' — a runner-network quirk that does not reproduce locally.
# The test cert's IP:127.0.0.1 SAN lets _validating_ssl_context() verify the
# chain against the loopback IP, so using '127.0.0.1' matches every other test
# in this module and keeps the suite green in CI.  See Sprint 52 R8.
@pytest.mark.asyncio
async def test_response_200(app):
    async with httpx.AsyncClient(http2=True, verify=_validating_ssl_context()) as c:
        res = await c.get(f'https://127.0.0.1:{app.port}/test', headers={'key': 'value'})
        assert res.status_code == 200


@pytest.mark.asyncio
async def test_response_404_fn(app):

    async with httpx.AsyncClient(http2=True, verify=_validating_ssl_context()) as c:
        res = await c.get(f'https://127.0.0.1:{app.port}/badpath', headers={'key': 'value'})

        assert res.status_code == 404
        assert res.content == b'not found test.'


@pytest.mark.asyncio
async def test_routing_middleware(app):
    logger.debug('test_routing_middleware is called.')

    async with httpx.AsyncClient(http2=True, verify=_validating_ssl_context()) as c:
        res = await c.get(f'https://127.0.0.1:{app.port}/test2', headers={'key': 'value'})

        assert res.status_code == 200
        assert res.content == b'fn3fn2fn1'


@pytest.mark.asyncio
async def test_websocket_response(app, ssl_context):
    # Connect explicitly to 127.0.0.1 (IPv4) instead of '::1' or 'localhost'.
    # On some systems 'localhost' resolves to '::1' (IPv6 loopback), but the
    # self-signed test certificate only covers the hostname 'localhost', not
    # the IP address '::1'.  Using the literal IPv4 address and overriding
    # server_hostname to 'localhost' avoids the verification error.
    uri = f"wss://127.0.0.1:{app.port}/websocket1"

    async with websockets.connect(uri, ssl=ssl_context,
                                  server_hostname='localhost') as client:
        logger.debug('Websocket has been connected.')

        name = 'Toshio'
        await asyncio.wait_for(client.send(name), timeout=5.0)
        logger.info('Have sent.')

        response = await asyncio.wait_for(client.recv(), timeout=5.0)
        assert response == name


# ---------------------------------------------------------------------------
# Lifespan startup / shutdown hook tests (unit — no running server)
# ---------------------------------------------------------------------------

def _make_lifespan_receive(*event_types):
    """Return an async callable that yields lifespan events in order."""
    queue = list(reversed([{'type': t} for t in event_types]))
    async def receive():
        return queue.pop()
    return receive


@pytest.mark.asyncio
async def test_on_startup_hook_called_at_lifespan_startup():
    app_ = BlackBull()
    called = []

    @app_.on_startup
    async def hook():
        called.append('startup')

    async def noop_send(_): pass
    receive = _make_lifespan_receive('lifespan.startup', 'lifespan.shutdown')
    await app_({'type': 'lifespan'}, receive, noop_send)
    assert called == ['startup']


@pytest.mark.asyncio
async def test_on_shutdown_hook_called_at_lifespan_shutdown():
    app_ = BlackBull()
    called = []

    @app_.on_shutdown
    async def hook():
        called.append('shutdown')

    async def noop_send2(_): pass
    receive = _make_lifespan_receive('lifespan.startup', 'lifespan.shutdown')
    await app_({'type': 'lifespan'}, receive, noop_send2)
    assert called == ['shutdown']


@pytest.mark.asyncio
async def test_multiple_startup_hooks_run_in_order():
    app_ = BlackBull()
    log = []

    @app_.on_startup
    async def hook_a():
        log.append('a')

    @app_.on_startup
    async def hook_b():
        log.append('b')

    async def noop_send3(_): pass
    receive = _make_lifespan_receive('lifespan.startup', 'lifespan.shutdown')
    await app_({'type': 'lifespan'}, receive, noop_send3)
    assert log == ['a', 'b']


@pytest.mark.asyncio
async def test_on_startup_handler_runs_via_dispatcher():
    """@app.on_startup fires once during lifespan startup (via dispatcher)."""
    app_ = BlackBull()
    called = []

    @app_.on_startup
    async def _():
        called.append('startup')

    async def noop_send_d(_): pass
    receive = _make_lifespan_receive('lifespan.startup', 'lifespan.shutdown')
    await app_({'type': 'lifespan'}, receive, noop_send_d)
    assert called == ['startup']


@pytest.mark.asyncio
async def test_on_intercept_app_startup_is_equivalent_to_on_startup():
    """@app.intercept('app_startup') receives an Event with name 'app_startup'."""
    from blackbull.event import Event
    app_ = BlackBull()
    received = []

    @app_.intercept('app_startup')
    async def _(event: Event):
        received.append(event)

    async def noop_send_e(_): pass
    receive = _make_lifespan_receive('lifespan.startup', 'lifespan.shutdown')
    await app_({'type': 'lifespan'}, receive, noop_send_e)
    assert len(received) == 1
    assert received[0].name == 'app_startup'


# ---------------------------------------------------------------------------
# app.py property and dispatch coverage
# ---------------------------------------------------------------------------

def test_loop_property_sync_context():
    app = BlackBull()
    # In a synchronous context no event loop is running → should return None
    assert app.loop is None


def test_certfile_property():
    app = BlackBull()
    assert app.certfile is None


def test_keyfile_property():
    app = BlackBull()
    assert app.keyfile is None


def test_ws_protocols_setter_encodes_strings():
    app = BlackBull()
    app.available_ws_protocols = ['h2', 'http/1.1']
    assert app.available_ws_protocols == [b'h2', b'http/1.1']


def test_ws_protocols_setter_keeps_bytes():
    app = BlackBull()
    app.available_ws_protocols = [b'h2']
    assert app.available_ws_protocols == [b'h2']


@pytest.mark.asyncio
async def test_dispatch_invalid_scheme_raises():
    app = BlackBull()
    scope = {'type': 'ftp', 'method': 'GET', 'path': '/', 'headers': []}
    with pytest.raises(Exception, match='Invalid scheme'):
        await app._dispatch(scope, AsyncMock(), AsyncMock())


@pytest.mark.asyncio
async def test_dispatch_websocket_no_handler_returns_silently():
    app = BlackBull()
    scope = {'type': 'websocket', 'path': '/ws', 'headers': []}
    # No handler registered — should return without raising
    await app._dispatch(scope, AsyncMock(), AsyncMock())


@pytest.mark.asyncio
async def test_dispatch_websocket_calls_handler():
    app = BlackBull()
    called = []

    @app.route(path='/ws', scheme=Scheme.websocket)
    async def ws_handler(scope, receive, send):
        called.append(True)

    scope = {'type': 'websocket', 'path': '/ws', 'headers': []}
    await app._dispatch(scope, AsyncMock(), AsyncMock())
    assert called == [True]


@pytest.mark.asyncio
async def test_raising_shutdown_hook_sends_lifespan_shutdown_failed():
    """ASGI lifespan spec: a raising shutdown hook must answer
    lifespan.shutdown with lifespan.shutdown.failed (+ message), not
    lifespan.shutdown.complete (bug 1.18)."""
    app_ = BlackBull()

    @app_.on_shutdown
    async def bad_hook():
        raise RuntimeError('shutdown hook boom')

    sent = []
    async def capture_send(event):
        sent.append(event)

    receive = _make_lifespan_receive('lifespan.startup', 'lifespan.shutdown')
    await app_({'type': 'lifespan'}, receive, capture_send)

    types = [e['type'] for e in sent]
    assert 'lifespan.shutdown.failed' in types
    assert 'lifespan.shutdown.complete' not in types
    failed = next(e for e in sent if e['type'] == 'lifespan.shutdown.failed')
    assert 'shutdown hook boom' in failed.get('message', '')
