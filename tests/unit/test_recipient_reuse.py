"""``HTTP1Recipient`` rebinding across keep-alive requests.

The actor builds one recipient per *connection* and points it at each request
in turn, the way it already reuses the sender.  What matters is that a rebound
recipient is indistinguishable from a fresh one: request N+1 must see its own
framing and its own body, never a remnant of request N.  Every test here
asserts that through the ASGI events the recipient emits, not through its
attributes.
"""
import pytest

from blackbull.connection import Connection
from blackbull.headers import Headers
from blackbull.server.recipient import AsyncioReader, HTTP1Recipient


class _Source:
    """Serves a byte stream; ``feed`` appends the next request's body."""

    def __init__(self, data: bytes = b''):
        self._d = bytearray(data)

    def feed(self, data: bytes) -> None:
        self._d += data

    async def read(self, n: int = -1) -> bytes:
        if n < 0:
            out, self._d = bytes(self._d), bytearray()
            return out
        out = bytes(self._d[:n])
        del self._d[:n]
        return out

    async def readuntil(self, sep: bytes = b'\n') -> bytes:
        idx = self._d.find(sep)
        if idx == -1:
            from blackbull.server.recipient import IncompleteReadError
            out, self._d = bytes(self._d), bytearray()
            raise IncompleteReadError(out)
        end = idx + len(sep)
        out = bytes(self._d[:end])
        del self._d[:end]
        return out

    async def readexactly(self, n: int) -> bytes:
        if len(self._d) < n:
            from blackbull.server.recipient import IncompleteReadError
            out, self._d = bytes(self._d), bytearray()
            raise IncompleteReadError(out)
        out = bytes(self._d[:n])
        del self._d[:n]
        return out


def _conn(headers: list[tuple[bytes, bytes]], path: str = '/p') -> Connection:
    return Connection(
        type='http', http_version='1.1', method='POST', path=path,
        raw_path=path.encode(), query_string=b'', headers=Headers(headers),
        scheme='http',
    )


@pytest.mark.asyncio
async def test_rebound_recipient_reads_the_second_request_body():
    """The reuse hazard in one test: a recipient that stayed ``_done`` would
    answer request N+1 with ``http.disconnect`` and the handler would see an
    empty body."""
    src = _Source(b'first')
    r = HTTP1Recipient(AsyncioReader(src), _conn([(b'content-length', b'5')]))

    assert (await r())['body'] == b'first'

    src.feed(b'second!')
    r.bind(_conn([(b'content-length', b'7')]))

    event = await r()
    assert event['type'] == 'http.request'
    assert event['body'] == b'second!'


@pytest.mark.asyncio
async def test_rebind_re_derives_framing_from_the_new_headers():
    """Content-Length → chunked on the next request: the framing comes from
    the new head, not the one the object was built with."""
    src = _Source(b'abc')
    r = HTTP1Recipient(AsyncioReader(src), _conn([(b'content-length', b'3')]))
    assert (await r())['body'] == b'abc'

    src.feed(b'4\r\nwxyz\r\n0\r\n\r\n')
    r.bind(_conn([(b'transfer-encoding', b'chunked')]))

    body = b''
    while True:
        event = await r()
        if event['type'] != 'http.request':
            break
        body += event['body']
        if not event.get('more_body'):
            break
    assert body == b'wxyz'


@pytest.mark.asyncio
async def test_rebind_clears_broken_framing():
    """``framing_broken`` closes the connection; it must not be inherited by a
    request that never broke anything."""
    r = HTTP1Recipient(AsyncioReader(_Source()), _conn([]))
    r.framing_broken = True

    r.bind(_conn([(b'content-length', b'2')]))

    assert r.framing_broken is False
    assert r.needs_drain() is True


@pytest.mark.asyncio
async def test_rebind_rejects_unsupported_transfer_encoding():
    """``__init__`` raises on an encoding we do not implement; rebinding is the
    same entry point for request N+1 and must not become a way past it."""
    r = HTTP1Recipient(AsyncioReader(_Source()), _conn([]))

    with pytest.raises(NotImplementedError):
        r.bind(_conn([(b'transfer-encoding', b'gzip')]))


@pytest.mark.asyncio
async def test_actor_builds_one_recipient_per_connection():
    """The reason the rebinding exists, asserted where the cost is paid."""
    from blackbull import BlackBull
    from blackbull.event_aggregator import EventAggregator
    from blackbull.server import recipient as recipient_mod
    from blackbull.server.http1_actor import HTTP1Actor

    app = BlackBull()

    @app.route(path='/keepalive')
    async def handler(conn):
        return 'ok'

    req = (b'GET /keepalive HTTP/1.1\r\nHost: x\r\n\r\n')
    src = _Source(req * 3)

    built = 0
    original = recipient_mod.HTTP1Recipient.__init__

    def counting_init(self, *a, **kw):
        nonlocal built
        built += 1
        return original(self, *a, **kw)

    from blackbull.server.sender import AbstractWriter

    class _NullWriter(AbstractWriter):
        async def write(self, data: bytes) -> None: pass

    recipient_mod.HTTP1Recipient.__init__ = counting_init
    try:
        actor = HTTP1Actor(AsyncioReader(src), _NullWriter(), app,
                           EventAggregator(app._dispatcher))
        await actor.run()
    finally:
        recipient_mod.HTTP1Recipient.__init__ = original

    assert built == 1
