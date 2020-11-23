import asyncio
import pytest
from unittest.mock import MagicMock

from BlackBull.logger import get_logger_set
logger, log = get_logger_set()

# Test targets
from BlackBull import BlackBull
# from BlackBull.util import update_scope


@pytest.fixture(scope="session", autouse=True)
def app():
    # run before the test
    app = BlackBull()

    @app.route(path='/test')
    def test_(scope, ctx):
        logger.debug('test_({}, {})'.format(scope, ctx))
        return str(scope) + str(ctx)

    yield app

    # run after the test

async def dummy_event():
    logger.debug('dummy_event()')
    event = update_event()
    logger.debug(event)
    return event


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
