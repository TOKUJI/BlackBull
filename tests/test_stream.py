import asyncio
import pytest
import json
import logging
import asyncio

from BlackBull.logger import get_logger_set, ColoredFormatter
logger, _ = get_logger_set('test_stream')

# Test targets
# from plugin import Plugin, LedgerInfo
from BlackBull.stream import Stream

def test_create():
    s = Stream(0, )


def test_add_child():
    s = Stream(0, )
    s.add_child(1)

def test_get_children():
    s = Stream(0, )
    c = s.add_child(1)
    assert s.get_children() == [c]

# @pytest.mark.asyncio
# async def test_condition():
#     s = Stream(0, )
#     c = s.add_child(1)
#     lock = c.get_lock()

#     async with lock:
#         await lock.wait()
#         logger.debug('in the lock')
#         lock.notify()
#         logger.debug('finish waiting')


