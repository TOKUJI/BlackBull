"""A dead connection must be discovered once, not once per stream.

``BaseSender`` records "the peer is gone" in ``self._closed``, which is
per-sender state.  It is a property of the *connection*: HTTP/2 builds one
sender per stream (``HTTP2Actor.make_sender``) over one shared writer, so every
stream rediscovers the dead socket by writing into it.  asyncio counts those
writes on the transport and logs a warning for each one past
``LOG_THRESHOLD_FOR_CONNLOST_WRITES`` (5).

That asymmetry is visible in HttpArena's published logs for this server: the
HTTP/1.1 lanes — one sender per connection — carry none of it, while a single
30-second ``baseline-h2`` run produced 264,278 lines of "SSL connection is
closed" and 4,415 of "socket.send() raised exception."
"""
from __future__ import annotations

import asyncio
import logging
import socket
import ssl
from pathlib import Path

import pytest

from blackbull.server.sender import AsyncioWriter, BaseSender

pytestmark = pytest.mark.asyncio

CERT = str(Path(__file__).parent.parent / 'cert.pem')
KEY = str(Path(__file__).parent.parent / 'key.pem')

STREAMS = 100


class _Sender(BaseSender):
    """``BaseSender`` is abstract only in its ASGI ``__call__``; the write path
    under test lives entirely in the base."""

    async def __call__(self, event):  # pragma: no cover - not exercised
        raise NotImplementedError


class _CapturingHandler(logging.Handler):
    def __init__(self):
        super().__init__()
        self.messages: list[str] = []

    def emit(self, record):
        self.messages.append(record.getMessage())


async def _accepted_writer(*, tls: bool):
    """A server-side writer whose peer aborted without a clean shutdown —
    what a load generator tearing down its connection pool looks like."""
    accepted: asyncio.Future = asyncio.get_running_loop().create_future()

    async def handle(reader, writer):
        accepted.set_result(writer)
        await asyncio.sleep(30)

    if tls:
        server_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        server_ctx.load_cert_chain(CERT, KEY)
        server = await asyncio.start_server(
            handle, '127.0.0.1', 0, ssl=server_ctx)
    else:
        server = await asyncio.start_server(handle, '127.0.0.1', 0)
    port = server.sockets[0].getsockname()[1]

    if tls:
        client_ctx = ssl.create_default_context()
        client_ctx.check_hostname = False
        client_ctx.verify_mode = ssl.CERT_NONE
        _reader, client_writer = await asyncio.open_connection(
            '127.0.0.1', port, ssl=client_ctx)
        stream_writer = await accepted
        client_writer.transport.abort()
    else:
        sock = socket.create_connection(('127.0.0.1', port))
        # SO_LINGER 0 => close() sends RST rather than FIN.
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER,
                        b'\x01\x00\x00\x00\x00\x00\x00\x00')
        stream_writer = await accepted
        sock.close()

    for _ in range(50):
        await asyncio.sleep(0.01)
        if stream_writer.transport.is_closing():
            break

    return server, stream_writer


async def _warnings_from_one_sender_per_stream(*, tls: bool) -> list[str]:
    server, stream_writer = await _accepted_writer(tls=tls)
    handler = _CapturingHandler()
    asyncio_logger = logging.getLogger('asyncio')
    asyncio_logger.addHandler(handler)
    previous = asyncio_logger.level
    asyncio_logger.setLevel(logging.WARNING)
    try:
        # One writer for the connection, one sender per stream — the shape
        # ``HTTP2Actor.make_sender(stream_id)`` produces.
        writer = AsyncioWriter(stream_writer)
        for _ in range(STREAMS):
            await _Sender(writer)._write(b'y' * 512)
            await asyncio.sleep(0)
    finally:
        asyncio_logger.removeHandler(handler)
        asyncio_logger.setLevel(previous)
        server.close()
        await server.wait_closed()
    return handler.messages


async def test_a_dead_tls_connection_is_discovered_once():
    """The lane the 264,278 log lines came from."""
    messages = await _warnings_from_one_sender_per_stream(tls=True)
    assert not messages, (
        f'{len(messages)} of {STREAMS} streams wrote into a transport already '
        f'known to be gone; first: {messages[0]!r}')


async def test_a_dead_plaintext_connection_is_discovered_once():
    """Same defect, the cleartext lane — 4,415 lines in the same run."""
    messages = await _warnings_from_one_sender_per_stream(tls=False)
    assert not messages, (
        f'{len(messages)} of {STREAMS} streams wrote into a transport already '
        f'known to be gone; first: {messages[0]!r}')


async def test_a_live_connection_still_writes():
    """Control: sharing the flag must not close a sender whose peer is here.

    Without this, "stop writing" would pass by never writing at all."""

    async def handle(reader, writer):
        shared = AsyncioWriter(writer)
        await _Sender(shared)._write(b'hello')
        await _Sender(shared)._write(b' world')
        writer.close()

    server = await asyncio.start_server(handle, '127.0.0.1', 0)
    port = server.sockets[0].getsockname()[1]
    try:
        reader, writer = await asyncio.open_connection('127.0.0.1', port)
        payload = await asyncio.wait_for(reader.read(-1), timeout=5)
        writer.close()
    finally:
        server.close()
        await server.wait_closed()

    assert payload == b'hello world'


async def _production_shaped_writer():
    """An ``AsyncioWriter`` over the object the server actually passes it.

    ``server.py`` builds ``AsyncioWriter(self, ...)`` where *self* is the
    ``ConnectionProtocol`` — not an ``asyncio.StreamWriter``.  The two differ in
    exactly the way this guard depends on: ``StreamWriter.drain()`` consults the
    reader's exception and raises once the peer is gone, while
    ``ConnectionProtocol.drain()`` raises only if ``connection_lost`` recorded
    one and otherwise returns immediately, and its ``write()`` hands the bytes
    to a transport that drops them silently.

    A test that uses a StreamWriter therefore passes on a guard that does
    nothing in production.  This one uses what production uses.
    """
    from blackbull.server.connection_protocol import ConnectionProtocol

    loop = asyncio.get_running_loop()
    accepted: asyncio.Future = loop.create_future()

    def factory():
        proto = ConnectionProtocol()
        original = proto.connection_made

        def connection_made(transport):
            original(transport)
            if not accepted.done():
                accepted.set_result(proto)

        proto.connection_made = connection_made
        return proto

    server = await loop.create_server(factory, '127.0.0.1', 0)
    port = server.sockets[0].getsockname()[1]

    sock = socket.create_connection(('127.0.0.1', port))
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER,
                    b'\x01\x00\x00\x00\x00\x00\x00\x00')
    proto = await accepted
    sock.close()

    for _ in range(50):
        await asyncio.sleep(0.01)
        if proto.transport is not None and proto.transport.is_closing():
            break

    return server, proto


async def test_the_guard_works_on_the_writer_the_server_actually_uses():
    """The production shape: no exception is ever raised, so a guard that waits
    for one never fires."""
    server, proto = await _production_shaped_writer()
    handler = _CapturingHandler()
    asyncio_logger = logging.getLogger('asyncio')
    asyncio_logger.addHandler(handler)
    previous = asyncio_logger.level
    asyncio_logger.setLevel(logging.WARNING)
    try:
        writer = AsyncioWriter(proto)
        for _ in range(STREAMS):
            await _Sender(writer)._write(b'y' * 512)
            await asyncio.sleep(0)
    finally:
        asyncio_logger.removeHandler(handler)
        asyncio_logger.setLevel(previous)
        server.close()
        await server.wait_closed()

    assert not handler.messages, (
        f'{len(handler.messages)} of {STREAMS} writes reached a transport '
        f'already gone; first: {handler.messages[0]!r}')


async def test_writes_between_transport_loss_and_connection_lost_are_stopped():
    """The window the exception-based guard cannot see.

    ``connection_lost`` is delivered through ``call_soon``, so the transport
    records the loss one or more loop turns before the protocol learns of it.
    In between, ``write()`` drops silently and ``drain()`` returns without
    raising — a guard that waits for an exception never fires, and every write
    in the window is one asyncio counts and warns about.  This is where the
    bulk of the volume came from: an EC2 run of all sixteen profiles on
    v0.78.1's first attempt still produced 610,210 "socket.send() raised
    exception." lines and 489,977 "SSL connection is closed", and one container
    log was truncated at 8 MB.
    """
    from blackbull.server.connection_protocol import ConnectionProtocol

    loop = asyncio.get_running_loop()
    accepted: asyncio.Future = loop.create_future()

    def factory():
        proto = ConnectionProtocol()
        original = proto.connection_made

        def connection_made(transport):
            original(transport)
            if not accepted.done():
                accepted.set_result(proto)

        proto.connection_made = connection_made
        return proto

    server = await loop.create_server(factory, '127.0.0.1', 0)
    port = server.sockets[0].getsockname()[1]
    sock = socket.create_connection(('127.0.0.1', port))
    proto = await accepted

    handler = _CapturingHandler()
    asyncio_logger = logging.getLogger('asyncio')
    asyncio_logger.addHandler(handler)
    previous = asyncio_logger.level
    asyncio_logger.setLevel(logging.WARNING)
    try:
        writer = AsyncioWriter(proto)
        # Lose the transport without letting connection_lost be delivered.
        proto.transport._force_close(ConnectionResetError(104, 'reset'))
        assert proto.transport.is_closing() is True
        assert proto._exc is None, 'the window closed before the test could use it'

        for _ in range(STREAMS):
            await _Sender(writer)._write(b'y' * 512)
    finally:
        asyncio_logger.removeHandler(handler)
        asyncio_logger.setLevel(previous)
        sock.close()
        server.close()
        await server.wait_closed()

    assert not handler.messages, (
        f'{len(handler.messages)} of {STREAMS} writes landed in the window '
        f'between transport loss and connection_lost; '
        f'first: {handler.messages[0]!r}')
