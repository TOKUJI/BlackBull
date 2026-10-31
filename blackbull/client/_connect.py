"""Bounded transport open, shared by every client.

A client reads and writes through the same buffered protocol the server uses.
"""
import asyncio
import ssl as _ssl

from ..server.connection_protocol import ConnectionProtocol

# Default connection-establishment deadline, including TLS.
DEFAULT_CONNECT_TIMEOUT: float = 30.0


class _TransportClose:
    async def _close_transport(self) -> None:
        connection, self._raw_writer = self._raw_writer, None
        if connection is not None:
            connection.close()
            await connection.wait_closed()


class _TransportOwner(_TransportClose):
    """Transfer an existing buffered transport only after session construction succeeds."""

    def _handoff(self, session_type, **options):
        session = session_type(self._reader, self._writer, self._raw_writer, **options)
        self._raw_writer = None
        return session


class ClientConnection(ConnectionProtocol):
    """The server's buffered protocol, plus a close a client can await."""

    def connection_made(self, transport) -> None:
        self._lost = asyncio.get_running_loop().create_future()
        return super().connection_made(transport)

    def connection_lost(self, exc: BaseException | None) -> None:
        super().connection_lost(exc)
        if not self._lost.done():
            self._lost.set_result(None)

    async def wait_closed(self) -> None:
        await self._lost


async def open_connection(
    host: str,
    port: int,
    ssl: _ssl.SSLContext | None,
    timeout: float | None,
) -> ClientConnection:
    """Connect under a deadline.

    Raises ``TimeoutError`` when the peer does not finish connecting in time --
    deliberately not wrapped in a client exception, so callers can tell a peer
    that stalled from one that answered and refused.  ``timeout=None`` opts out
    and restores the unbounded wait for callers imposing their own deadline.
    """
    coro = asyncio.get_running_loop().create_connection(
        ClientConnection, host, port, ssl=ssl)
    if timeout is None:
        _transport, conn = await coro
    else:
        async with asyncio.timeout(timeout):
            _transport, conn = await coro
    return conn
