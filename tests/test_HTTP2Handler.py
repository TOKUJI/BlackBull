import asyncio
import pytest
import json
import logging
import asyncio
import io

from blackbull.logger import get_logger_set, ColoredFormatter
logger, _ = get_logger_set('test_HTTP2Handler')

# Test targets
from blackbull.server import HTTP2Handler
from blackbull.utils import HTTP2

in_ = io.BytesIO(HTTP2)
out_ = io.BytesIO(b"")


def test_():

    pass
