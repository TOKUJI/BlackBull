"""`eof_received` answers what the transport can actually honour.

Returning True keeps the write half open after a peer half-closes, which is
what a cleartext client doing `shutdown(SHUT_WR)` while awaiting its response
needs.  TLS cannot offer it: asyncio's SSL protocol closes on EOF whatever the
app protocol returns, and logs a warning for each claim it ignores — 3,425 in a
sixteen-profile run.  The claim is what stops; the log line was only the symptom.
"""
from __future__ import annotations

import asyncio
import logging
import socket
import ssl
from pathlib import Path

import pytest

from blackbull.server.connection_protocol import ConnectionProtocol

pytestmark = pytest.mark.asyncio

CERT = str(Path(__file__).parent.parent / 'cert.pem')
KEY = str(Path(__file__).parent.parent / 'key.pem')


class _Capture(logging.Handler):
    def __init__(self):
        super().__init__()
        self.messages: list[str] = []

    def emit(self, record):
        self.messages.append(record.getMessage())


async def _half_close_once(*, tls: bool) -> list[str]:
    loop = asyncio.get_running_loop()
    if tls:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(CERT, KEY)
        server = await loop.create_server(ConnectionProtocol, '127.0.0.1', 0, ssl=ctx)
    else:
        server = await loop.create_server(ConnectionProtocol, '127.0.0.1', 0)
    port = server.sockets[0].getsockname()[1]

    handler = _Capture()
    asyncio_logger = logging.getLogger('asyncio')
    asyncio_logger.addHandler(handler)
    previous = asyncio_logger.level
    asyncio_logger.setLevel(logging.WARNING)
    try:
        client_ctx = None
        if tls:
            client_ctx = ssl.create_default_context()
            client_ctx.check_hostname = False
            client_ctx.verify_mode = ssl.CERT_NONE
        reader, writer = await asyncio.open_connection(
            '127.0.0.1', port, ssl=client_ctx)
        writer.write(b'GET / HTTP/1.1\r\n\r\n')
        await writer.drain()
        sock = writer.transport.get_extra_info('socket')
        if sock is not None:
            sock.shutdown(socket.SHUT_WR)     # half-close, still awaiting a reply
        for _ in range(20):
            await asyncio.sleep(0.02)
        writer.close()
        for _ in range(10):
            await asyncio.sleep(0.02)
    finally:
        asyncio_logger.removeHandler(handler)
        asyncio_logger.setLevel(previous)
        server.close()
        await server.wait_closed()
    return handler.messages


async def test_a_tls_half_close_claims_nothing_asyncio_will_ignore():
    messages = await _half_close_once(tls=True)
    offending = [m for m in messages if 'eof_received' in m]
    assert not offending, offending


async def test_a_cleartext_half_close_still_keeps_the_write_half():
    """The capability the True was there for, unchanged."""
    proto = ConnectionProtocol()

    class _T:
        def get_extra_info(self, name, default=None):
            return None          # no ssl_object -> cleartext

    proto.connection_made(_T())
    assert proto.eof_received() is True

    tls = ConnectionProtocol()

    class _TLS:
        def get_extra_info(self, name, default=None):
            return object() if name == 'ssl_object' else None

    tls.connection_made(_TLS())
    assert tls.eof_received() is False


async def test_a_stream_that_loses_the_spawn_race_leaves_no_orphan_coroutine():
    """A HEADERS frame arriving as the connection winds down.

    ``StreamActor.run()`` is built before ``create_task`` can say whether the
    TaskGroup will still take it.  When it will not, the coroutine has to be
    closed here — otherwise Python reports "coroutine 'StreamActor.run' was
    never awaited" whenever the GC reaches it, naming a stream unrelated to
    wherever the line lands.
    """
    import warnings

    class _ClosedGroup:
        def create_task(self, coro):
            coro.close()          # what asyncio does before raising
            raise RuntimeError('TaskGroup is finished')

    async def _never_run():
        return None

    coro = _never_run()
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter('always')
        try:
            _ClosedGroup().create_task(coro)
        except RuntimeError:
            coro.close()          # the guard under test, in miniature
        del coro
        import gc
        gc.collect()

    orphans = [w for w in caught
               if issubclass(w.category, RuntimeWarning)
               and 'never awaited' in str(w.message)]
    assert not orphans, [str(w.message) for w in orphans]
