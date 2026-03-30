import time
import asyncio
import pytest
import concurrent.futures
import sys
import socket
import time
import subprocess
import pathlib
from blackbull.frame import FrameFactory, FrameTypes
from blackbull.logger import get_logger_set
logger, log = get_logger_set()

# Test targets
from blackbull.client import Client
from blackbull.utils import EventEmitter
cwd = pathlib.Path(__file__).parent

@pytest.fixture(scope="session")
def server():
    process = subprocess.Popen(['python3', 'main.py'],
                               cwd=cwd / '..')

    timeout = 10
    start = time.time()
    while True:
        try:
            with socket.create_connection(('localhost', 8000), timeout=1):
                break
        except (ConnectionRefusedError, OSError):
            if time.time() - start > timeout:
                process.terminate()
                raise RuntimeError("Server did not start within timeout")
            time.sleep(0.1)

    yield process

    process.terminate()


@pytest.mark.asyncio
async def test_connect(server):
    asyncio.get_running_loop().set_debug(True)
    c = Client(name='test', port=8000)

    is_connected = c.is_connected()
    assert is_connected is False

    await c.connect()
    is_connected = c.is_connected()
    assert is_connected is True

    c.disconnect()
    is_connected = c.is_connected()
    assert is_connected is False


# @pytest.mark.asyncio
# async def test_stream():
#     c = Client(name='test', port=8000)
#     s1 = c.get_stream()
#     s2 = c.get_stream()
#     s = c.find_stream(s1.identifier)
#     assert s1 == s


# if __name__ == '__main__':
#     print('main')
#     from blackbull.logger import get_logger_set
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
