import json
from http import HTTPStatus

from blackbull.logger import get_logger_set

# Library for test-fixture
# from multiprocessing import Process
import asyncio
import pytest

# Test targets
from blackbull import Response, JSONResponse, WebSocketResponse
from blackbull.response import make_body, make_websocket_body

# Library for tests
# import httpx

logger, log = get_logger_set()


class Dummy:
    """
    Mock of send object. It holds data in its instance.
    """
    def __init__(self, *args, **kwargs):
        self.data = None

    async def __call__(self, data):
        self.data = data


@pytest.fixture
async def send():
    logger.info('At teardown.')

    send = Dummy()
    yield send

    logger.info('At teardown.')


@pytest.mark.asyncio
async def test_Response(send):
    assert send.data != b'text'

    await Response(send, b'text')

    assert send.data == make_body(b'text')


@pytest.mark.asyncio
async def test_Response_str(send):
    assert send.data != b'text'

    await Response(send, 'text')

    assert send.data == make_body(b'text')


@pytest.mark.asyncio
async def test_JSONResponse(send):
    assert send.data != b'text'

    obj = {'x': list(set([str(), int()]))}   # Example of complicated object.

    await JSONResponse(send, obj)

    assert send.data == make_body(json.dumps(obj).encode())


@pytest.mark.asyncio
async def test_WebSocketResponse(send):
    assert send.data != b'text'

    obj = {'x': list(set([str(), int()]))}   # Example of complicated object.

    await WebSocketResponse(send, obj)

    assert send.data == make_websocket_body(json.dumps(obj))
