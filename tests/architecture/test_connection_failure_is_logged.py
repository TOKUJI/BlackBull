"""A connection whose protocol handler raises leaves an error record behind."""
import asyncio
import logging

import pytest

from blackbull import BlackBull
from blackbull.server import Server
from blackbull.server.listener import Listener, Tcp


@pytest.mark.asyncio
async def test_a_raising_handler_is_logged(caplog):
    caplog.set_level(logging.ERROR, logger='blackbull.server.connection_actor')
    app = BlackBull()

    @app.raw_handler('boom', port=0)
    async def boom(reader, writer, ctx):
        raise RuntimeError('handler blew up')

    server = Server(app, listeners=[Listener(Tcp(0, '127.0.0.1'))])
    server.open_socket()
    running = asyncio.create_task(server.run())
    reader, writer = await asyncio.open_connection('127.0.0.1', server.protocol_ports['boom'])
    assert await asyncio.wait_for(reader.read(), 5) == b''
    writer.close()
    await server.stop(drain_timeout=5.0)
    await running
    [record] = [r for r in caplog.records if r.name == 'blackbull.server.connection_actor']
    assert record.exc_info and 'handler blew up' in str(record.exc_info[1])
