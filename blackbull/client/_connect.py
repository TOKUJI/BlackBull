"""Bounded transport open, shared by every client.

A leaf module on purpose: every client imports it, so it must import nothing
back out of the package.
"""
import asyncio
import ssl as _ssl

# How long a client may spend establishing a connection before giving up.
#
# This is the *time* column of the limit triad; a transport open has no size
# dimension, and the unit/total columns belong to the response-reading path
# instead.  A bare open_connection() has no deadline of its own -- TLS
# negotiation in particular can stall for the lifetime of the process -- so
# leaving it unset is an unbounded wait, not a generous one.
#
# 30 s matches the server's own BB_BODY_TIMEOUT default: the same order as the
# other time bounds in this tree, and far above any healthy handshake.
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
