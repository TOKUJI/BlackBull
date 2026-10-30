"""A connection whose protocol handler raises leaves an error record behind."""
import asyncio
import logging

import pytest

from blackbull import BlackBull
from blackbull.server import Server
from blackbull.server.listener import Listener, Tcp


@pytest.mark.asyncio
@pytest.mark.parametrize('error', [
    RuntimeError('handler blew up'),
    ExceptionGroup('mixed', [ConnectionResetError(), RuntimeError('handler blew up')]),
], ids=['plain', 'with-a-peer-reset'])
async def test_a_raising_handler_is_logged(caplog, error):
    caplog.set_level(logging.ERROR, logger='blackbull.server.connection_actor')
    app = BlackBull()

    @app.raw_handler('boom', port=0)
    async def boom(reader, writer, ctx):
        raise error

    server = Server(app, listeners=[Listener(Tcp(0, '127.0.0.1'))])
    server.open_socket()
    running = asyncio.create_task(server.run())
    reader, writer = await asyncio.open_connection('127.0.0.1', server.protocol_ports['boom'])
    assert await asyncio.wait_for(reader.read(), 5) == b''
    writer.close()
    await server.stop(drain_timeout=5.0)
    await running
    [record] = [r for r in caplog.records if r.name == 'blackbull.server.connection_actor']
    assert record.exc_info and 'handler blew up' in repr(record.exc_info[1])
