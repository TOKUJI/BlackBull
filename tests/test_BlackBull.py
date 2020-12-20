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
from blackbull.utils import Scheme
from blackbull.middlewares import websocket

# Library for tests
import httpx
import websockets

logger, log = get_logger_set()


def run_application(app):
    logger.info('dummy is called.')
    loop = asyncio.new_event_loop()
    task = loop.create_task(app.run())
    loop.run_until_complete(task)


@pytest.fixture  # (scope="session", autouse=True)
async def app():
    # run before the test
    logger.info('At set-up.')
    app = BlackBull()
    app.create_server(certfile='cert.pem', keyfile='key.pem')

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
        msg = await receive()
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

    @app.route(path='/websocket2', scheme=Scheme.websocket)
    async def websocket2(scope, receive, send):
        while msg := (await receive()):
            if 'text' in msg:
                logger.debug(f'Got a text massage ({msg}.)')
            elif 'bytes' in msg:
                logger.debug(f'Got a byte-string massage ({msg}.)')
            else:
                logger.info('The received message does not contain any message.')
                break

            await WebSocketResponse(send, msg)

    app.route(path='/websocket2', scheme=Scheme.websocket,
              functions=[websocket, websocket2])

    p = Process(target=run_application, args=(app,))
    p.start()

    yield app

    logger.info('At teardown.')
    app.stop()
    p.terminate()


@pytest.fixture
async def ssl_context():
    ssl_context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    logger.error(pathlib.Path(__file__))
    localhost_pem = pathlib.Path(__file__).with_name("cert.pem")
    ssl_context.load_verify_locations(localhost_pem)

    yield ssl_context

    # At tear down.
    pass


@pytest.mark.asyncio
async def test_response_200(app):
    async with httpx.AsyncClient(http2=True, verify=False) as c:
        res = await c.get(f'https://localhost:{app.port}/test', headers={'key': 'value'})
        assert res.status_code == 200


@pytest.mark.asyncio
async def test_response_404_fn(app):

    async with httpx.AsyncClient(http2=True, verify=False) as c:
        res = await c.get(f'https://localhost:{app.port}/badpath', headers={'key': 'value'})

        assert res.status_code == 404
        assert res.content == b'not found test.'


@pytest.mark.asyncio
async def test_routing_middleware(app):
    logger.info('Registered.')
    async with httpx.AsyncClient(http2=True, verify=False) as c:
        res = await c.get(f'https://localhost:{app.port}/test2', headers={'key': 'value'})

        assert res.status_code == 200
        assert res.content == b'fn3fn2fn1'


@pytest.mark.asyncio
async def test_websocket_response(app, ssl_context):
    uri = f"wss://localhost:{app.port}/websocket"
    client = await asyncio.wait_for(websockets.connect(uri, ssl=ssl_context), timeout=0.5)

    async with client:
        name = 'Toshio'
        await client.send(name)
        logger.error('Have sent.')

        greeting = await asyncio.wait_for(client.recv(), timeout=0.5)
        assert greeting == name
