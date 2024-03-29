from http import HTTPStatus
import ssl
import pathlib

from blackbull.logger import get_logger_set

# Library for test-fixture
from multiprocessing import Process
import asyncio
import pytest

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


@pytest.fixture
async def app():
    # run before the test
    logger.info('At set-up.')
    app = BlackBull()
    cd = pathlib.Path(__file__).parent
    app.create_server(certfile=cd / 'cert.pem', keyfile=cd / 'key.pem')

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
            if 'text' in msg:
                logger.debug(f'Got a text massage ({msg}.)')
            elif 'bytes' in msg:
                logger.debug(f'Got a byte-string massage ({msg}.)')
            else:
                logger.info('The received message does not contain any message.')
                break

            await WebSocketResponse(send, msg)

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

    yield app

    logger.info('At teardown.')
    app.stop()
    p.terminate()


@pytest.fixture
async def ssl_context():
    ssl_context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    logger.info(pathlib.Path(__file__))
    localhost_pem = pathlib.Path(__file__).with_name("cert.pem")
    ssl_context.load_verify_locations(localhost_pem)

    yield ssl_context

    # At tear down.
    pass


@pytest.fixture
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
    uri = f"wss://localhost:{app.port}/websocket1"

    async with websockets.connect(uri, ssl=ssl_context) as client:
        logger.debug('Websocket has been connected.')

        name = 'Toshio'
        await asyncio.wait_for(client.send(name), timeout=0.1)
        logger.info('Have sent.')

        response = await asyncio.wait_for(client.recv(), timeout=0.1)
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
