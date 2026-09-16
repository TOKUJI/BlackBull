"""The two readers over WebSocket frame payload refuse every line read.

``HTTP2WSReader`` (server) and ``_H2QueueReader`` (HTTP/2 WebSocket client)
carry WebSocket frames, which have no line framing, so a line-oriented read on
either is a caller error.  The refusal is ``NotImplementedError`` in every call
shape ``AbstractReader`` admits — ``readuntil`` bounded and unbounded,
``read_head`` at every limit, and the same two through ``PrefixReader(b'')``.

Each reader holds a complete head followed by EOF, fed through its public
input, so a call that answered with bytes returns instead of waiting forever.
"""
import asyncio

import pytest

from blackbull.client.websocket_h2 import _H2QueueReader
from blackbull.protocol.frame import FrameFactory
from blackbull.protocol.frame_types import DataFrameFlags, FrameTypes
from blackbull.server.http2_ws import HTTP2WSReader
from blackbull.server.recipient import PrefixReader

pytestmark = pytest.mark.asyncio

HEAD = b'GET / HTTP/1.1\r\nhost: x\r\n\r\n'
CALL_TIMEOUT_S = 1.0


async def _ws_reader():
    reader = HTTP2WSReader()
    reader.put_DATAFrame(FrameFactory().create(FrameTypes.DATA, 0, 1, data=HEAD))
    reader.put_disconnect()
    return reader


async def _queue_reader():
    queue = asyncio.Queue()
    await queue.put(FrameFactory().create(
        FrameTypes.DATA, DataFrameFlags.END_STREAM, 1, data=HEAD))

    async def credit(n):
        pass

    return _H2QueueReader(queue, credit)


READERS = [pytest.param(_ws_reader, id='HTTP2WSReader'),
           pytest.param(_queue_reader, id='_H2QueueReader')]


async def _refuses(call):
    with pytest.raises(NotImplementedError):
        await asyncio.wait_for(call, CALL_TIMEOUT_S)


@pytest.mark.parametrize('make_reader', READERS)
async def test_readuntil_with_a_positional_limit_is_refused(make_reader):
    reader = await make_reader()
    await _refuses(reader.readuntil(b'\r\n', 5))


@pytest.mark.parametrize('make_reader', READERS)
async def test_readuntil_with_a_keyword_limit_is_refused(make_reader):
    reader = await make_reader()
    await _refuses(reader.readuntil(b'\r\n', limit=5))


@pytest.mark.parametrize('make_reader', READERS)
async def test_bounded_read_head_is_refused(make_reader):
    reader = await make_reader()
    await _refuses(reader.read_head(1024))


@pytest.mark.parametrize('make_reader', READERS)
async def test_unbounded_readuntil_is_refused(make_reader):
    reader = await make_reader()
    await _refuses(reader.readuntil(b'\r\n'))


@pytest.mark.parametrize('make_reader', READERS)
async def test_unbounded_read_head_is_refused(make_reader):
    reader = await make_reader()
    await _refuses(reader.read_head(0))


@pytest.mark.parametrize('make_reader', READERS)
async def test_bounded_readuntil_through_an_empty_prefix_is_refused(make_reader):
    reader = PrefixReader(b'', await make_reader())
    await _refuses(reader.readuntil(b'\r\n', 64))


@pytest.mark.parametrize('make_reader', READERS)
async def test_bounded_read_head_through_an_empty_prefix_is_refused(make_reader):
    reader = PrefixReader(b'', await make_reader())
    await _refuses(reader.read_head(1024))
