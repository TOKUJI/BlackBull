"""Regression test for the AF_UNIX guard in ``SocketManager``.

On platforms without Unix-domain socket support (notably some Windows
builds), ``socket.AF_UNIX`` does not exist as a module attribute.  Before
the guard was added, ``SocketManager`` dereferenced it unconditionally on
every accepted socket family check and crashed with ``AttributeError`` —
making BlackBull unusable on Windows.
"""
import asyncio
import socket as _socket

import pytest

from blackbull.server.server import SocketManager


@pytest.mark.asyncio
async def test_socket_manager_handles_missing_af_unix(monkeypatch):
    monkeypatch.delattr(_socket, 'AF_UNIX', raising=False)
    assert not hasattr(_socket, 'AF_UNIX')

    tcp_sock = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
    tcp_sock.setsockopt(_socket.SOL_SOCKET, _socket.SO_REUSEADDR, 1)
    tcp_sock.bind(('127.0.0.1', 0))
    tcp_sock.setblocking(False)

    async def _cb(reader, writer):
        writer.close()

    try:
        async with SocketManager([(tcp_sock, _cb)], ssl_context=None) as servers:
            assert len(servers) == 1
            await asyncio.sleep(0)
    finally:
        tcp_sock.close()


@pytest.mark.asyncio
@pytest.mark.timeout(30)
async def test_server_serves_tcp_with_af_unix_missing(monkeypatch):
    """Opening accepting checks AF_UNIX listeners; a TCP-only server must
    start without the constant."""
    from blackbull import BlackBull
    from blackbull.server.server import Server

    app = BlackBull()

    @app.route(path='/')
    async def _index():
        return 'ok'

    server = Server(app)
    server.open_socket(0)
    monkeypatch.delattr(_socket, 'AF_UNIX', raising=False)
    runner = asyncio.create_task(server.run())
    try:
        deadline = asyncio.get_running_loop().time() + 5
        while True:
            assert not runner.done(), runner.exception()
            try:
                reader, writer = await asyncio.open_connection('127.0.0.1', server.port)
                break
            except OSError:
                assert asyncio.get_running_loop().time() < deadline
                await asyncio.sleep(0.02)
        writer.write(b'GET / HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n')
        assert (await asyncio.wait_for(reader.read(), 5)).startswith(b'HTTP/1.1 200')
        writer.close()
    finally:
        await asyncio.wait_for(server.stop(drain_timeout=1.0), timeout=5)
        await asyncio.wait({runner}, timeout=5)
        server.close_socket()
