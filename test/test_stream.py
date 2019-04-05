import asyncio
import pytest
import json
import logging

from BlackBull.logger import get_logger_set, ColoredFormatter
logger, log = get_logger_set('test_plugin')

# Test targets
# from plugin import Plugin, LedgerInfo
from BlackBull.frame import Stream

def test_create():
    s = Stream(0, )


def test_add_child():
    s = Stream(0, )
    s.add_child(1)

def test_get_children():
    s = Stream(0, )
    c = s.add_child(1)
    assert s.get_children() == [c]

