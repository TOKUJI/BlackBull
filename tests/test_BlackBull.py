from http import HTTPStatus

from BlackBull.logger import get_logger_set

# Library for test-fixture
from multiprocessing import Process
import asyncio
import pytest

# Test targets
from BlackBull import BlackBull
from BlackBull.response import respond
# from BlackBull.stream import Stream

# Library for tests
import httpx

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
    app.create_server()

    @app.route(path='/test')
    async def test_(scope, receive, send, **kwargs):
        logger.debug(f'test_({scope}, {receive}, {send}, {kwargs})')
        await respond(send, 'sample')

    @app.route_404
    async def test_404(scope, receive, send, **kwargs):
        logger.debug(f'test_404({scope}, {receive}, {send}, {kwargs})')
        await respond(send, b'not found test.', status=HTTPStatus.NOT_FOUND)

    p = Process(target=run_application, args=(app,))
    p.start()

    yield app

    logger.info('At teardown.')
    app.stop()
    p.terminate()

# async def dummy_event():
#     logger.debug('dummy_event()')
#     event = update_event()
#     logger.debug(event)
#     return event


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
async def test_add_route_during_running(app):
    # Check the result here because app does not return any value
    path = 'test2'

    @app.route(path='/test2')
    async def test2(scope, ctx):
        logger.debug('test_({}, {})'.format(scope, ctx))
        return str(scope) + str(ctx)

    async with httpx.AsyncClient(http2=True, verify=False) as c:
        res = await c.get(f'https://localhost:{app.port}/{path}', headers={'key': 'value'})
        assert res.status_code == 200
