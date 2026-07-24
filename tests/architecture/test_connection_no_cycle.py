"""Regression guard: a request must not leave a per-request reference cycle.

v0.60.0 threaded the native :class:`Connection` end to end but bound the
disconnect-detecting *wrapper* onto ``conn._receive`` (for lazy ``conn.body()``).
That closed a cycle — ``conn._receive`` → wrapper → (captured) ``conn``, plus
``conn`` → recipient → ``conn`` — so the whole per-request graph could only be
reclaimed by the generational cyclic GC. Its periodic pauses were the v0.60.0
tail-latency regression (see
``.claude/planning/research/v0600-regression-investigation.md`` §6).

The fix binds the *raw* recipient (which references no ``conn``) via
``bind_receive_channel`` and stops the router overwriting it with the wrapper.
These tests drive real requests with the cyclic collector disabled and assert
that refcounting alone reclaims each request's graph — ``gc.collect()`` then
finds essentially nothing to do.

Sensitivity: on the pre-fix code the body-less GET hot path leaves ~21
cycle-objects per request, so the ``< 1.0 objs/req`` bound cleanly separates
fixed from regressed.
"""
import asyncio
import gc
from http import HTTPMethod

import pytest

from blackbull import BlackBull
from blackbull.server.http1_actor import HTTP1Actor
from blackbull.server.recipient import AbstractReader
from blackbull.server.sender import AbstractWriter


# --------------------------------------------------------------------------
# Minimal in-memory transport (mirrors test_request_disconnected_event.py)
# --------------------------------------------------------------------------
class _Writer(AbstractWriter):
    def __init__(self):
        self.written = bytearray()

    async def write(self, data: bytes) -> None:
        self.written += data


class _Reader(AbstractReader):
    def __init__(self, data: bytes = b''):
        self._buf = bytearray(data)

    async def read(self, n: int = -1) -> bytes:
        if n < 0:
            b, self._buf = bytes(self._buf), bytearray()
            return b
        b = bytes(self._buf[:n])
        del self._buf[:n]
        return b

    async def readuntil(self, sep: bytes) -> bytes:
        i = self._buf.find(sep)
        if i == -1:
            b, self._buf = bytes(self._buf), bytearray()
            return b + sep
        b = bytes(self._buf[:i + len(sep)])
        del self._buf[:i + len(sep)]
        return b

    async def readexactly(self, n: int) -> bytes:
        if len(self._buf) < n:
            raise asyncio.IncompleteReadError(bytes(self._buf), n)
        b = bytes(self._buf[:n])
        del self._buf[:n]
        return b


def _app() -> BlackBull:
    app = BlackBull()

    @app.route(path='/ping')
    async def ping():                       # body-less baseline hot path
        return b'pong'

    @app.route(path='/echo', methods=[HTTPMethod.POST])
    async def echo(request):                # drains via request.body() → conn._receive
        return await request.body()

    return app


def _raw_get(path='/ping') -> bytes:
    return f'GET {path} HTTP/1.1\r\nHost: x\r\n\r\n'.encode()


def _raw_post(path='/echo', body=b'hello') -> bytes:
    return (f'POST {path} HTTP/1.1\r\nHost: x\r\n'
            f'Content-Length: {len(body)}\r\n\r\n').encode() + body


async def _drive_h1(app: BlackBull, raw: bytes) -> None:
    actor = HTTP1Actor(_Reader(b''), _Writer(), app, None, request=raw,
                       peername=('127.0.0.1', 5), sockname=('0.0.0.0', 8000))
    await actor.run()


async def _census(driver, n: int) -> float:
    """Return per-request cycle-objects reclaimed by gc after *n* driven requests
    run with the cyclic collector disabled."""
    await driver()                          # warm registration/import graphs
    gc.collect()
    gc.disable()
    try:
        for _ in range(n):
            await driver()
        collected = gc.collect()
    finally:
        gc.enable()
    return collected / n


@pytest.mark.asyncio
async def test_native_h1_body_less_get_leaves_no_cycle():
    app = _app()
    per_req = await _census(lambda: _drive_h1(app, _raw_get()), n=500)
    assert per_req < 1.0, (
        f'body-less GET leaves {per_req:.1f} cycle-objs/request — the v0.60.0 '
        f'per-request reference cycle has been reintroduced')


@pytest.mark.asyncio
async def test_native_h1_request_body_read_leaves_no_cycle():
    app = _app()
    per_req = await _census(lambda: _drive_h1(app, _raw_post()), n=500)
    assert per_req < 1.0, (
        f'POST reading request.body() leaves {per_req:.1f} cycle-objs/request — '
        f'conn._receive is cycling through the recipient again')


@pytest.mark.asyncio
async def test_external_asgi_path_leaves_no_cycle():
    """The external-ASGI entry (uvicorn / httpx.ASGITransport) builds the
    Connection via ``from_scope`` and binds the host's own receive channel;
    that path must not cycle either."""
    app = _app()

    async def _drive_external():
        scope = {
            'type': 'http', 'asgi': {'version': '3.0'}, 'http_version': '1.1',
            'method': 'GET', 'scheme': 'http', 'path': '/ping', 'raw_path': b'/ping',
            'query_string': b'', 'root_path': '',
            'headers': [(b'host', b'x')], 'client': ('127.0.0.1', 5),
            'server': ('localhost', 80),
        }
        events = [{'type': 'http.request', 'body': b'', 'more_body': False},
                  {'type': 'http.disconnect'}]
        it = iter(events)

        async def receive():
            return next(it, {'type': 'http.disconnect'})

        async def send(_event):
            return None

        await app(scope, receive, send)

    per_req = await _census(_drive_external, n=500)
    assert per_req < 1.0, (
        f'external-ASGI request leaves {per_req:.1f} cycle-objs/request')
