"""A peer that resets the transport is a normal end of a connection.

RST-closing clients (SO_LINGER=0, load balancers, connection churn) must not
produce an ERROR record with a formatted traceback per connection: formatting
it dominated the per-connection cost under churn.
"""
import asyncio
import logging
import socket
import struct

import pytest

from blackbull import BlackBull
from blackbull.server import ASGIServer


@pytest.mark.asyncio
async def test_peer_reset_is_not_logged_as_connection_failure(caplog):
    app = BlackBull()

    @app.route(path='/')
    async def root():
        return 'ok'

    server = ASGIServer(app)
    sock = socket.socket()
    sock.bind(('127.0.0.1', 0))
    port = sock.getsockname()[1]
    sock.close()
    task = asyncio.create_task(server.run(port=port))
    try:
        for _ in range(100):
            try:
                r, w = await asyncio.open_connection('127.0.0.1', port)
                break
            except OSError:
                await asyncio.sleep(0.02)
        caplog.set_level(logging.DEBUG, logger='blackbull.server.connection_actor')
        w.write(b'GET / HTTP/1.1\r\nHost: x\r\n\r\n')
        await w.drain()
        await r.readuntil(b'ok')
        raw = w.get_extra_info('socket')
        raw.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack('ii', 1, 0))
        w.transport.abort()          # RST, as gcannon -r / many LBs do
        await asyncio.sleep(0.2)
        errors = [rec for rec in caplog.records
                  if rec.name == 'blackbull.server.connection_actor'
                  and rec.levelno >= logging.ERROR]
        assert errors == []
    finally:
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass
