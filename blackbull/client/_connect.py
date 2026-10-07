"""Bounded transport open, shared by every client.

A leaf module on purpose: every client imports it, so it must import nothing
back out of the package.
"""
import asyncio
import ssl as _ssl

# Default connection-establishment deadline, including TLS.
DEFAULT_CONNECT_TIMEOUT: float = 30.0


async def open_connection(
    host: str,
    port: int,
    ssl: _ssl.SSLContext | None,
    timeout: float | None,
) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    """``asyncio.open_connection`` under a deadline.

    Raises ``TimeoutError`` when the peer does not finish connecting in time --
    deliberately not wrapped in a client exception, so callers can tell a peer
    that stalled from one that answered and refused.  ``timeout=None`` opts out
    and restores the unbounded wait for callers imposing their own deadline.
    """
    coro = asyncio.open_connection(host, port, ssl=ssl)
    if timeout is None:
        return await coro
    async with asyncio.timeout(timeout):
        return await coro
