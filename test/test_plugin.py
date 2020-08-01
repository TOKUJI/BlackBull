import asyncio
import pytest
import json
import logging

from BlackBull.logger import get_logger_set, ColoredFormatter
logger, log = get_logger_set('test_plugin')

# Test targets
from plugin import Plugin, LedgerInfo


@pytest.fixture(scope="session", autouse=True)
def run_server():
    from subprocess import Popen
    # process = Popen(['daphne', 'main:app'])
    process = Popen(['python', 'main.py'])
    import time
    time.sleep(2)

    yield process

    process.terminate()


# def test_marshaling():
#     l = LedgerInfo(prefix='aaa')
#     s = json.dumps(l, cls=LedgerInfo.Encoder)
#     logger.info(s)
#     a = json.loads(s, cls=LedgerInfo.Decoder)
#     logger.info(a)
#     assert l == a


@pytest.mark.asyncio
async def test_info():
    c = Plugin(name='test', port=8000)
    await c.connect()
    f = c.listen()
    info = await c.get_info()
    assert type(info) == LedgerInfo
    assert info.prefix == 'local.Abank'

    c.disconnect()


# @pytest.mark.asyncio
# async def test_login():
#     c = Plugin(name='test', port=8000)
#     await c.connect()
#     f = c.listen()
#     result = await c.login('alice', 'alice')
#     assert c.is_login == True
#     c.disconnect()



# @pytest.mark.asyncio
# async def test_balance():
#     c = Plugin(name='test', port=8000)
#     await c.connect()
#     await c.login('alice', 'alice')
#     balance = await c.get_balance()
#     assert balance == 1000
#     c.disconnect()
# 

# @pytest.mark.asyncio
# async def test_account(run_server):
#     c = Plugin(name='alice', port=8000)
#     await c.connect()
#     c.listen()
#     await c.login('alice', 'alice')
#     res = await c.get_account()
#     logger.info(res)
#     c.disconnect()



# @pytest.mark.asyncio
# async def test_fulfillment():
#     pass


# @pytest.mark.asyncio
# async def test_send_transfer():
#     pass


# @pytest.mark.asyncio
# async def test_fulfillment():
#     pass

# @pytest.mark.asyncio
# async def test_send_request():
#     pass

# @pytest.mark.asyncio
# async def test_fulfill_condition():
#     pass

# @pytest.mark.asyncio
# async def test_reject_incoming_transfer():
#     pass

# @pytest.mark.asyncio
# async def test_register_request_handler():
#     pass

# @pytest.mark.asyncio
# async def test_deregister_request_handler():
#     pass
