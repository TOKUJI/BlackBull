import time
import asyncio
import pytest
import concurrent.futures

from BlackBull.frame import FrameFactory, FrameTypes
from BlackBull.logger import get_logger_set
logger, log = get_logger_set()

# Test targets
from BlackBull.client import Client
from BlackBull.util import EventEmitter



@pytest.fixture(scope="session", autouse=False)
def server():
    from subprocess import Popen
    process = Popen(['python', 'main.py'])
    time.sleep(2)

    yield process

    process.terminate()


@pytest.mark.asyncio
async def test_connect(server):
    asyncio.get_running_loop().set_debug(True)
    c = Client(name='test', port=8000)

    is_connected = c.is_connected()
    assert is_connected == False

    await c.connect()
    is_connected = c.is_connected()
    assert is_connected == True

    c.disconnect()
    is_connected = c.is_connected()
    assert is_connected == False


# @pytest.mark.asyncio
# async def test_stream():
#     c = Client(name='test', port=8000)
#     s1 = c.get_stream()
#     s2 = c.get_stream()
#     s = c.find_stream(s1.identifier)
#     assert s1 == s


# if __name__ == '__main__':
#     print('main')
#     from BlackBull.logger import get_logger_set
#     logger, log = get_logger_set('test')
#     logging.getLogger("asyncio").setLevel(logging.DEBUG)
#     logger.setLevel(logging.DEBUG)
#     handler = logging.StreamHandler()
#     handler.setLevel(logging.WARNING)
#     cf = ColoredFormatter('%(levelname)-12s:%(name)s:%(lineno)d %(message)s')
#     handler.setFormatter(cf)
#     logger.addHandler(handler)

#     fh = logging.FileHandler('test.log',)
#     fh.setLevel(logging.DEBUG)
#     ff = logging.Formatter('%(levelname)-12s:%(name)s:%(lineno)d %(message)s')
#     fh.setFormatter(ff)
#     logger.addHandler(fh)
