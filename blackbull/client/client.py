"""ALPN-dispatching front door."""
import ssl as _ssl
from typing import Union

import logging
from ._connect import DEFAULT_CONNECT_TIMEOUT, open_connection as _open_connection
from .http1 import HTTP1Client
from .http2 import HTTP2Client

logger = logging.getLogger(__name__)


# Type alias: whichever inner client the dispatcher selected.
NegotiatedClient = Union[HTTP1Client, HTTP2Client]


class Client:
    """ALPN-negotiating client: ``HTTP2Client`` on ``h2``, else ``HTTP1Client``.

    With ``ssl=None`` (the default) there is no ALPN to read, so the answer is
    always HTTP/1.1.  WebSocket is never dispatched to; name its client.

    Use as an async context manager::

        async with Client('localhost', 8000) as c:
            res = await c.request(HTTPMethod.GET, '/')
    """

    def __init__(self, host: str, port: int, *,
                 ssl: _ssl.SSLContext | None = None,
                 connect_timeout: float | None = DEFAULT_CONNECT_TIMEOUT) -> None:
        self._host = host
        self._port = port
        self._ssl = ssl
        self._connect_timeout = connect_timeout
        self._inner: NegotiatedClient | None = None

    async def __aenter__(self) -> NegotiatedClient:
        ctx = self._ssl
        # Caller owns ALPN configuration on the SSL context.  When ``ssl`` is
        # ``None`` the dispatcher falls back to HTTP/1.1; when a context is
        # provided, the caller must configure ALPN with the protocols they
        # want negotiated (typically ``['h2', 'http/1.1']``).
        r, w = await _open_connection(self._host, self._port, ctx,
                                      self._connect_timeout)

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
