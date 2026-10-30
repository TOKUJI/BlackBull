"""A peer that resets the transport is a normal end of a connection.

RST-closing clients (SO_LINGER=0, load balancers, connection churn) must not
produce an ERROR record with a formatted traceback per connection, from any
logger, on either protocol, whether the response was complete or not.
"""
import asyncio
import gc
import logging
import socket
import struct

import pytest
from hpack import Encoder

from blackbull import BlackBull
from blackbull.protocol.frame_types import FrameTypes
from tests.conformance.http2._harness import _make_h2_frame, h2_connect, h2_section
from tests.harness import Wire, read_h2_frame, served

BODY = b'x' * (4 << 20)
_WINDOW = (1 << 31) - 1


async def _slow_reader(server) -> Wire:
    """A client whose small receive buffer leaves the server waiting to drain."""
    sock = socket.socket()
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 65536)
    sock.setblocking(False)
    await asyncio.get_running_loop().sock_connect(sock, ('127.0.0.1', server.server.port))
    wire = Wire(*await asyncio.open_connection(sock=sock))
    server.wires.append(wire)
    return wire


async def _request_h2(wire, *, streams=(1,), preface=b'') -> None:
    encoder = Encoder()
    await wire.send(preface + b''.join(h2_section(encoder.encode(
        [(b':method', b'GET'), (b':path', b'/'), (b':scheme', b'http'),
         (b':authority', b'x')]), sid=sid) for sid in streams))


#: Prior knowledge, with the largest windows, so content is limited by the socket.
_OPEN_WINDOWS = (b'PRI * HTTP/2.0\r\n\r\nSM\r\n\r\n'
                 + _make_h2_frame(FrameTypes.SETTINGS, 0, 0,
                                  (0x4).to_bytes(2, 'big') + _WINDOW.to_bytes(4, 'big'))
                 + _make_h2_frame(FrameTypes.WINDOW_UPDATE, 0, 0,
                                  (_WINDOW - 65535).to_bytes(4, 'big')))


@pytest.mark.asyncio
@pytest.mark.parametrize('when', ['after', 'during'])
@pytest.mark.parametrize('protocol', ['http1', 'http2'])
async def test_peer_reset_is_not_logged_as_a_failure(caplog, protocol, when):
    app = BlackBull()

    @app.route(path='/')
    async def root():
        return BODY if when == 'during' else 'ok'

    caplog.set_level(logging.WARNING)
    loop = asyncio.get_running_loop()
    unhandled = []
    loop.set_exception_handler(lambda _loop, context: unhandled.append(context['message']))
    try:
        async with served(app) as server:
            if when == 'during':
                wire = await _slow_reader(server)
                if protocol == 'http1':
                    await wire.send(b'GET / HTTP/1.1\r\nHost: x\r\n\r\n')
                else:
                    await _request_h2(wire, streams=(1, 3), preface=_OPEN_WINDOWS)
                await wire.recv_exactly(30_000)
            elif protocol == 'http1':
                wire = await server.wire()
                await wire.send(b'GET / HTTP/1.1\r\nHost: x\r\n\r\n')
                await wire.response()
            else:
                wire = await h2_connect(server)
                await _request_h2(wire)
                while True:
                    kind, flags, sid, _ = await read_h2_frame(wire.reader)
                    if sid == 1 and kind in (0, 1) and flags & 1:
                        break
            raw = wire.writer.get_extra_info('socket')
            raw.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack('ii', 1, 0))
            wire.writer.transport.abort()          # RST, as gcannon -r / many LBs do
            await asyncio.sleep(0.2)
        gc.collect()
        await asyncio.sleep(0)
    finally:
        loop.set_exception_handler(None)
    assert unhandled == []
    errors = [(rec.name, rec.getMessage()) for rec in caplog.records
              if rec.levelno >= logging.ERROR]
    assert errors == []
