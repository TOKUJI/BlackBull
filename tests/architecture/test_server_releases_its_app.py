"""A stopped Server, and the app it served, are freed once nothing uses them."""
import asyncio
import gc
import logging
import weakref

import pytest

from blackbull import BlackBull
from blackbull.server import Server
from blackbull.server.listener import Listener, Tcp


async def _serve_one_request():
    app = BlackBull()

    @app.route(path='/')
    async def root():
        return b'ok'

    server = Server(app, listeners=[Listener(Tcp(0, '127.0.0.1'))])
    server.open_socket()
    running = asyncio.create_task(server.run())
    reader, writer = await asyncio.open_connection('127.0.0.1', server.port)
    writer.write(b'GET / HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n')
    await writer.drain()
    assert (await reader.read()).startswith(b'HTTP/1.1 200 ')
    writer.close()
    await server.stop(drain_timeout=5.0)
    await running
    return weakref.ref(server), weakref.ref(app)


@pytest.mark.asyncio
async def test_a_stopped_server_and_its_app_are_freed():
    logging.disable(logging.CRITICAL)   # a captured record would pin its arguments
    try:
        refs = [await _serve_one_request() for _ in range(3)]
    finally:
        logging.disable(logging.NOTSET)
    gc.collect()
    assert [ref() for pair in refs for ref in pair] == [None] * 6
