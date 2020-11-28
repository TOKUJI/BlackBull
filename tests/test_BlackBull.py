from multiprocessing import Process
import asyncio
import pytest
# from unittest.mock import MagicMock

from BlackBull.logger import get_logger_set

import httpx
# Test targets
from BlackBull import BlackBull
# from BlackBull.stream import Stream

# socket.bind(('', 0)) will be assigned an available port.
logger, log = get_logger_set()


def dummy(app):
    logger.info('dummy is called.')
    loop = asyncio.new_event_loop()
    task = loop.create_task(app.run())
    loop.run_until_complete(task)


@pytest.fixture  # (scope="session", autouse=True)
async def app():
    # run before the test
    app = BlackBull()
    app.create_server()

    @app.route(path='/test')
    async def test_(scope, ctx):
        logger.debug('test_({}, {})'.format(scope, ctx))
        return str(scope) + str(ctx)

    p = Process(target=dummy, args=(app,))
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
async def test_connect(app):
    logger.info(app)
    async with httpx.AsyncClient(http2=True, verify=False) as c:

        logger.info('Start sending requests.')

        logger.info(f'https://localhost:{app.port}/test')
        first = c.get(f'https://localhost:{app.port}/test', headers={'key': 'value'})

        logger.info(f'https://localhost:{app.port}/test')
        second = c.post(f'https://localhost:{app.port}/test', data=b'hello')

        logger.info(f'https://localhost:{app.port}/test')
        third = c.get(f'https://localhost:{app.port}/test')

        logger.info('All requests have been sent.')

        first_response = await first
        logger.info('Got response for the first request.')
        logger.info(first_response)

        second_response = await second
        logger.info('Got response for the second request.')
        logger.info(second_response)

        third_response = await third
        logger.info('Got response for the third request.')
        logger.info(third_response)

    # Create an client, then open connection().

# @pytest.mark.asyncio
# async def test_route(app):
#     # scope = MagicMock(update_scope())
#     scope = update_scope()
#     scope['path'] = '/test'

#     # Check the result here because app does not return any value
#     async def assert_here(b):
#         logger.debug(b)
#         return b

#     await app(scope)(dummy_event, assert_here)
