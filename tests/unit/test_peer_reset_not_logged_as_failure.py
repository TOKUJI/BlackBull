"""A peer that resets the transport is a normal end of a connection.

RST-closing clients (SO_LINGER=0, load balancers, connection churn) must not
produce an ERROR record with a formatted traceback per connection, on either
protocol.
"""
import asyncio
import logging
import socket
import struct

import pytest
from hpack import Encoder

from blackbull import BlackBull
from tests.conformance.http2._harness import h2_connect, h2_section
from tests.harness import read_h2_frame, served


@pytest.mark.asyncio
@pytest.mark.parametrize('protocol', ['http1', 'http2'])
async def test_peer_reset_is_not_logged_as_connection_failure(caplog, protocol):
    app = BlackBull()

    @app.route(path='/')
    async def root():
        return 'ok'

    caplog.set_level(logging.DEBUG, logger='blackbull.server.connection_actor')
    async with served(app) as server:
        if protocol == 'http1':
            wire = await server.wire()
            await wire.send(b'GET / HTTP/1.1\r\nHost: x\r\n\r\n')
            await wire.response()
        else:
            wire = await h2_connect(server)
            await wire.send(h2_section(Encoder().encode(
                [(b':method', b'GET'), (b':path', b'/'), (b':scheme', b'http'),
                 (b':authority', b'x')])))
            while True:
                kind, flags, sid, _ = await read_h2_frame(wire.reader)
                if sid == 1 and kind in (0, 1) and flags & 1:
                    break
        raw = wire.writer.get_extra_info('socket')
        raw.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack('ii', 1, 0))
        wire.writer.transport.abort()          # RST, as gcannon -r / many LBs do
        await asyncio.sleep(0.2)
    errors = [rec for rec in caplog.records
              if rec.name == 'blackbull.server.connection_actor'
              and rec.levelno >= logging.ERROR]
    assert errors == []
