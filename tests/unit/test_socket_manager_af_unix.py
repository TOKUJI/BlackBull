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
