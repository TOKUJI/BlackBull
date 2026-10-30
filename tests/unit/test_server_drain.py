"""What ``Server.stop`` leaves behind once ``run()`` has returned.

A connection still owed its response at the drain deadline is cut, even when
the peer has stopped reading and the response sits in the write buffer; one
whose peer reads it in time is not.
"""
from __future__ import annotations

import asyncio
import os
import socket

import pytest
from hpack import Encoder

from blackbull.protocol.frame_types import FrameTypes
from blackbull.server import Server
from blackbull.server.listener import Listener, Tcp
from tests.conformance.http2._harness import _make_h2_frame, h2_section

pytestmark = [pytest.mark.asyncio, pytest.mark.timeout(60)]

BODY = b'x' * (32 << 20)
_WINDOW = (1 << 31) - 1


async def _app(scope, receive, send):
    if isinstance(scope, dict) and scope.get('type') == 'lifespan':
        return
    await send({'type': 'http.response.start', 'status': 200, 'headers': []})
    await send({'type': 'http.response.body', 'body': BODY})


def _open_descriptors() -> int:
    return len(os.listdir('/dev/fd'))


async def _client(port: int):
    sock = socket.socket()
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
    sock.setblocking(False)
    await asyncio.get_running_loop().sock_connect(sock, ('127.0.0.1', port))
    return await asyncio.open_connection(sock=sock, limit=4096)


async def _request(protocol: str, writer) -> None:
    if protocol == 'http1':
        writer.write(b'GET / HTTP/1.1\r\nHost: x\r\n\r\n')
    else:
        settings = (0x4).to_bytes(2, 'big') + _WINDOW.to_bytes(4, 'big')
        block = Encoder().encode([(b':method', b'GET'), (b':path', b'/'),
                                  (b':scheme', b'http'), (b':authority', b'x')])
        writer.write(b'PRI * HTTP/2.0\r\n\r\nSM\r\n\r\n'
                     + _make_h2_frame(FrameTypes.SETTINGS, 0, 0, settings)
                     + _make_h2_frame(FrameTypes.WINDOW_UPDATE, 0, 0,
                                      (_WINDOW - 65535).to_bytes(4, 'big'))
                     + h2_section(block))
    await writer.drain()


async def _received(reader) -> int:
    """Bytes read until the whole body could have arrived, or EOF."""
    total = 0
    while total < len(BODY) and (chunk := await reader.read(1 << 20)):
        total += len(chunk)
    return total


@pytest.mark.parametrize('protocol', ['http1', 'http2'])
async def test_a_peer_that_stops_reading_is_cut_at_the_drain_deadline(protocol):
    before = _open_descriptors()
    server = Server(_app, listeners=[Listener(Tcp(0, '127.0.0.1'))])
    server.open_socket()
    running = asyncio.create_task(server.run())
    reader, writer = await _client(server.port)
    try:
        await _request(protocol, writer)
        await reader.read(1)    # the response has started
        await asyncio.sleep(0.2)
        await server.stop(drain_timeout=0.5)
        await asyncio.wait_for(running, 10)
        # The client's own socket is the only one left.
        assert _open_descriptors() == before + 1
    finally:
        writer.close()


@pytest.mark.parametrize('protocol', ['http1', 'http2'])
async def test_a_peer_that_reads_within_the_drain_deadline_gets_everything(protocol):
    before = _open_descriptors()
    server = Server(_app, listeners=[Listener(Tcp(0, '127.0.0.1'))])
    server.open_socket()
    running = asyncio.create_task(server.run())
    reader, writer = await _client(server.port)
    try:
        await _request(protocol, writer)
        await reader.read(1)
        await asyncio.sleep(0.2)
        stopping = asyncio.create_task(server.stop(drain_timeout=10.0))
        received = await asyncio.wait_for(_received(reader), 20)
        writer.close()
        await stopping
        await asyncio.wait_for(running, 10)
        assert received + 1 > len(BODY)
        assert _open_descriptors() == before
    finally:
        writer.close()


async def _parked(scope, receive, send):
    if isinstance(scope, dict) and scope.get('type') == 'lifespan':
        return
    await asyncio.Event().wait()


@pytest.mark.parametrize('protocol', ['http1', 'http2'])
async def test_a_handler_still_running_at_the_drain_deadline_is_cut_cleanly(protocol):
    before = _open_descriptors()
    server = Server(_parked, listeners=[Listener(Tcp(0, '127.0.0.1'))])
    server.open_socket()
    running = asyncio.create_task(server.run())
    _reader, writer = await _client(server.port)
    try:
        await _request(protocol, writer)
        await asyncio.sleep(0.2)
        await server.stop(drain_timeout=0.5)
        await asyncio.wait_for(running, 10)
        assert _open_descriptors() == before + 1
    finally:
        writer.close()
