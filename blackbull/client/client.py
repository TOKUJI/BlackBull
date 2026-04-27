"""ALPN-dispatching front door.

``Client`` opens a TCP/TLS connection, inspects the negotiated ALPN
protocol, and hands off to ``HTTP2Client`` (when the server advertises
``h2``) or ``HTTP1Client`` (otherwise).

WebSocket is intentionally **not** part of ALPN dispatch — it is an
HTTP/1.1 upgrade and the caller picks it explicitly via
:class:`blackbull.client.WebSocketClient`.
"""
import asyncio
import ssl as _ssl
from typing import Union

import logging
from .http1 import HTTP1Client
from .http2 import HTTP2Client

logger = logging.getLogger(__name__)


# Type alias: whichever inner client the dispatcher selected.
NegotiatedClient = Union[HTTP1Client, HTTP2Client]


class Client:
    """ALPN-negotiating client.

    Picks ``HTTP2Client`` if the server advertises ``h2``, else ``HTTP1Client``.
    With ``ssl=None`` (the default), no ALPN is performed and HTTP/1.1 is used
    — h2c (HTTP/2 over plaintext) is supported by ``HTTP2Client`` directly,
    but the dispatcher only triggers it when ALPN selects ``h2``.

    Use as an async context manager::

        async with Client('localhost', 8000) as c:
            res = await c.request(HTTPMethod.GET, '/')
            # `c` is an HTTP1Client when ssl=None, or whichever the ALPN handshake chose.
    """

    def __init__(self, host: str, port: int, *,
                 ssl: _ssl.SSLContext | None = None) -> None:
        self._host = host
        self._port = port
        self._ssl = ssl
        self._inner: NegotiatedClient | None = None

    async def __aenter__(self) -> NegotiatedClient:
        ctx = self._ssl
        # Caller owns ALPN configuration on the SSL context.  When ``ssl`` is
        # ``None`` the dispatcher falls back to HTTP/1.1; when a context is
        # provided, the caller must configure ALPN with the protocols they
        # want negotiated (typically ``['h2', 'http/1.1']``).
        r, w = await asyncio.open_connection(self._host, self._port, ssl=ctx)

        proto: str | None = None
        if ctx is not None:
            ssl_obj = w.get_extra_info('ssl_object')
            if ssl_obj is not None:
                proto = ssl_obj.selected_alpn_protocol()

        inner: NegotiatedClient
        if proto == 'h2':
            inner = HTTP2Client._adopt(self._host, self._port, r, w, ssl=ctx)
        else:
            inner = HTTP1Client._adopt(self._host, self._port, r, w, ssl=ctx)
        await inner._start()

        self._inner = inner
        return inner

    async def __aexit__(self, exc_type, exc, tb) -> None:
        if self._inner is not None:
            await self._inner.__aexit__(exc_type, exc, tb)
            self._inner = None
