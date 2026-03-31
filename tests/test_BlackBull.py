from http import HTTPStatus
import ssl
import pathlib

from blackbull.logger import get_logger_set

# Library for test-fixture
from multiprocessing import Process
import asyncio
import pytest
import pytest_asyncio

# Test targets
from blackbull import BlackBull, Response, WebSocketResponse
from blackbull.utils import Scheme, HTTPMethods
# from blackbull.middlewares import websocket

# Library for tests
import httpx
import websockets

logger, log = get_logger_set()


def run_application(app):
    logger.info('dummy is called.')
    loop = asyncio.new_event_loop()
    task = loop.create_task(app.run())
    loop.run_until_complete(task)


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
    cd = pathlib.Path(__file__).parent
    cert_path = cd / 'cert.pem'
    key_path = cd / 'key.pem'

    app = BlackBull()

    logger.info(f"[app fixture] cert.pem exists: {cert_path.exists()}, key.pem exists: {key_path.exists()}")

    app.create_server(certfile=cert_path, keyfile=key_path, port=8000)

    # Routing not using middleware.
    @app.route(path='/test')
    async def test_(scope, receive, send):
        logger.debug(f'test_({scope}, {receive}, {send})')
        await Response(send, 'sample')

    @app.route_404
    async def test_404(scope, receive, send):
        logger.debug(f'test_404({scope}, {receive}, {send})')
        await Response(send, 'not found test.', status=HTTPStatus.NOT_FOUND)

    # Routing using middleware.
    async def test_fn1(scope, receive, send, inner):
        logger.info('test_fn1 starts.')
        res = await inner(scope, receive, send)
        logger.info(f'test_fn1 ends. res = {res}')
        await Response(send, res + 'fn1')

    async def test_fn2(scope, receive, send, inner):
        logger.info('test_fn2 starts.')
        res = await inner(scope, receive, send)
        logger.info(f'test_fn2 ends. res = {res}')
        return res + 'fn2'

    async def test_fn3(scope, receive, send, inner):
        logger.info('test_fn3 starts.')
        await inner(scope, receive, send)
        logger.info('test_fn3 ends.')
        return 'fn3'

    app.route(methods='get', path='/test2', functions=[test_fn1, test_fn2, test_fn3])

    @app.route(path='/websocket1', scheme=Scheme.websocket)
    async def websocket1(scope, receive, send):
        accept = {"type": "websocket.accept", "subprotocol": None}
        # msg = await receive()
        await send(accept)

        while msg := (await receive()):
            if msg.get('type') == 'websocket.disconnect':
                break
            if 'text' in msg and msg['text'] is not None:
                logger.debug(f'Got a text massage ({msg}.)')
                await WebSocketResponse(send, msg['text'])
            elif 'bytes' in msg and msg['bytes'] is not None:
                logger.debug(f'Got a byte-string massage ({msg}.)')
                await WebSocketResponse(send, msg['bytes'])
            else:
                logger.info('The received message does not contain any message.')
                break

        await send({'type': 'websocket.close'})

    async def websocket2(scope, receive, send):
        while msg := (await receive()):
            await WebSocketResponse(send, msg)

    app.route(path='/websocket2', scheme=Scheme.websocket,
              functions=[websocket2])

    @app.route(path='/push', methods=[HTTPMethods.post])
    async def server_push(scope, receive, send):
        # await Response(send, 'Any message?', more_body=True)
        request = await receive()

        while request['type'] != 'http.disconnect' and request['body'] != 'Bye':
            msg = request['body']
            await Response(send, msg, more_body=True)

            try:
                request = await asyncio.wait_for(receive(), timeout=0.5)

            except asyncio.TimeoutError:
                logger.debug('Have not received any message in this second.')
                await Response(send, 'Any message?', more_body=True)

    p = Process(target=run_application, args=(app,))
    p.start()
    app.wait_for_port(timeout=10.0)

    # Wait until the server is actually accepting connections before yielding
    # to the test.  Without this, the test races against server startup and
    # gets a ConnectionRefusedError or a silent TimeoutError.
    await wait_for_server('127.0.0.1', app.port)

    yield app

    logger.info('At teardown.')
    app.stop()
    p.terminate()


@pytest_asyncio.fixture
async def ssl_context():
    ssl_context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    logger.info(pathlib.Path(__file__))
    localhost_pem = pathlib.Path(__file__).with_name("cert.pem")
    ssl_context.load_verify_locations(localhost_pem)

    yield ssl_context

    # At tear down.
    pass


@pytest_asyncio.fixture
async def ssl_h2context():
    ssl_context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    logger.info(pathlib.Path(__file__))
    localhost_pem = pathlib.Path(__file__).with_name("cert.pem")
    ssl_context.load_verify_locations(localhost_pem)
    ssl_context.set_alpn_protocols(['h2'])

    yield ssl_context

    # At tear down.
    pass


# @pytest.mark.asyncio
# async def test_response_200(app):
#     async with httpx.AsyncClient(http2=True, verify=False) as c:
#         res = await c.get(f'https://localhost:{app.port}/test', headers={'key': 'value'})
#         assert res.status_code == 200


# @pytest.mark.asyncio
# async def test_response_404_fn(app):

#     async with httpx.AsyncClient(http2=True, verify=False) as c:
#         res = await c.get(f'https://localhost:{app.port}/badpath', headers={'key': 'value'})

#         assert res.status_code == 404
#         assert res.content == b'not found test.'


# @pytest.mark.asyncio
# async def test_routing_middleware(app):

#     async with httpx.AsyncClient(http2=True, verify=False) as c:
#         res = await c.get(f'https://localhost:{app.port}/test2', headers={'key': 'value'})

#         assert res.status_code == 200
#         assert res.content == b'fn3fn2fn1'


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

# @pytest.mark.asyncio
# async def test_http2_server_push(app, ssl_context):
#     uri = f'127.0.0.1:{app.port}'
#     msg = b'hello'
#     with HTTPConnection(uri, secure=True, enable_push=True, ssl_context=ssl_context) as conn:
#         conn.request('post', '/http2', body=msg)

#         for push in conn.get_pushes():  # all pushes promised before response headers
#             logger.info(push.path)

#         response = conn.get_response()
#         assert response.read() == msg

#         for push in conn.get_pushes():  # all other pushes
#             logger.info(push.path)
