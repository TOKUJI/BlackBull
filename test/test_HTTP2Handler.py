import asyncio
import pytest
import json
import logging
import asyncio
import io

from BlackBull.logger import get_logger_set, ColoredFormatter
logger, _ = get_logger_set('test_HTTP2Handler')

# Test targets
from BlackBull.server import HTTP2Handler
from ..util import HTTP2

in_ = io.ByteIO(HTTP2)
out_= io.ByteIO("")

def test_():
    
    h = HTTP2Hander()

    pass
