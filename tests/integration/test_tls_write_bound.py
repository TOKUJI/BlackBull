"""``BB_WRITE_TIMEOUT`` bounds a TLS response to a peer that stops reading (BLA-522).

A reading peer still receives every byte.  Both run on the selector loop and on
uvloop, over HTTP/1.1 and HTTP/2.
"""
import asyncio
import hashlib
import logging
import socket
import ssl
import struct
import time

import pytest

from blackbull import BlackBull
from blackbull.client.http1 import HTTP1Client
from blackbull.client.http2 import HTTP2Client
from blackbull.env import reset_settings_cache

from tests.harness import served

BODY = hashlib.shake_256(b'bla-522').digest(8 * 1024 * 1024)
#: The bound is one second; this is the generous outer limit for observing it.
WAIT = 5.0


def _runners():
    yield pytest.param(asyncio.run, id='asyncio')
    try:
        import uvloop
    except ImportError:
        yield pytest.param(None, id='uvloop', marks=pytest.mark.skip('uvloop missing'))
    else:
        yield pytest.param(uvloop.run, id='uvloop')


RUNNERS = list(_runners())


@pytest.fixture(autouse=True)
def _write_timeout(monkeypatch):
    monkeypatch.setenv('BB_WRITE_TIMEOUT', '1')
    reset_settings_cache()
    yield
    reset_settings_cache()


def _app() -> BlackBull:
    app = BlackBull()

    @app.route(path='/')
    async def root():
        return BODY

    return app


def _client_context(alpn: str) -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    ctx.check_hostname, ctx.verify_mode = False, ssl.CERT_NONE
    ctx.set_alpn_protocols([alpn])
    return ctx


def _frame(kind: int, flags: int, stream: int, payload: bytes) -> bytes:
    return struct.pack('>I', len(payload))[1:] + bytes((kind, flags)) + struct.pack('>I', stream) + payload


#: Preface, SETTINGS(INITIAL_WINDOW_SIZE = 2^31-1, MAX_FRAME_SIZE = 2^24-1), a
#: connection WINDOW_UPDATE to the same window, and HEADERS for
#: ``GET https://x/`` (static-table HPACK): flow control never stops the
#: server before TCP does, and one DATA frame carries the whole body.
H2_REQUEST = (
    b'PRI * HTTP/2.0\r\n\r\nSM\r\n\r\n'
    + _frame(0x4, 0, 0, struct.pack('>HIHI', 0x4, 2**31 - 1, 0x5, 2**24 - 1))
    + _frame(0x8, 0, 0, struct.pack('>I', 2**31 - 1 - 65535))
    + _frame(0x1, 0x5, 1, bytes((0x82, 0x87, 0x84, 0x41, 1)) + b'x'))
H1_REQUEST = b'GET / HTTP/1.1\r\nHost: x\r\n\r\n'


async def _stall(port: int, alpn: str, request: bytes) -> float | None:
    """Send *request*, never read, and return how long the server took to
    give up on the write; ``None`` if it had not within ``WAIT``."""
    logger = logging.getLogger('blackbull')
    hits = []
    handler = logging.Handler()
    handler.emit = lambda record: hits.append(record) if 'write timeout' in record.getMessage() else None
    logger.addHandler(handler)
    raw = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    raw.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
    raw.setblocking(False)
    loop = asyncio.get_running_loop()
    try:
        await loop.sock_connect(raw, ('127.0.0.1', port))
        _, writer = await asyncio.open_connection(
            sock=raw, ssl=_client_context(alpn), server_hostname='127.0.0.1')
        start = time.monotonic()
        writer.write(request)
        while not hits and time.monotonic() - start < WAIT:
            await asyncio.sleep(0.05)
        writer.transport.abort()
        return time.monotonic() - start if hits else None
    finally:
        logger.removeHandler(handler)


@pytest.mark.parametrize('run', RUNNERS)
@pytest.mark.parametrize('alpn,request_bytes', [('http/1.1', H1_REQUEST), ('h2', H2_REQUEST)],
                         ids=['http1', 'http2'])
def test_a_stalled_tls_peer_meets_the_write_timeout(run, alpn, request_bytes):
    async def go():
        async with served(_app(), tls=True) as s:
            return await _stall(s.server.port, alpn, request_bytes)

    elapsed = run(go())
    assert elapsed is not None, 'the stalled TLS peer outlived BB_WRITE_TIMEOUT'


@pytest.mark.parametrize('run', RUNNERS)
@pytest.mark.parametrize('client,alpn', [(HTTP1Client, 'http/1.1'), (HTTP2Client, 'h2')],
                         ids=['http1', 'http2'])
def test_a_reading_tls_peer_receives_the_whole_body(run, client, alpn):
    async def go():
        async with served(_app(), tls=True) as s:
            async with client('127.0.0.1', s.server.port, ssl=_client_context(alpn)) as c:
                return await c.request('GET', '/')

    response = run(go())
    assert (response.status, hashlib.sha256(response.body).digest()) == (
        200, hashlib.sha256(BODY).digest())
