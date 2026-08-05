"""`StaticFiles` is a framework-owned producer — it emits native, not dicts.

The single-world policy: nothing inside the seam constructs an ASGI event
dict.  `StaticFiles` was the last framework producer that did, and it is the
reason a second conversion altitude (`_boundary_wrap`) had to exist at all —
middleware-generated events bypassed the handler-boundary adapter, so a
converter was bolted on per global middleware.

Every response shape `StaticFiles` can produce is covered, because the shape
is what decides whether the native path applies:

  * cache hit          — header + terminal body in one object
  * sendfile           — header + `file_path` (the pathsend function, no dict)
  * chunked streaming  — header arm, then body chunks (Range / no-sendfile)
  * `_respond`         — 404 / 304 / 416, header + empty body

Asserting *what type* reaches the send channel, not just that the bytes are
right: dicts produce correct bytes too, which is how the conversion survived.
"""
import asyncio
import os
import pathlib

import pytest

from blackbull import BlackBull
from blackbull.middleware.static import StaticFiles
from blackbull.native import NativeResponse
from blackbull.testing import NativeTestServer

_SMALL = b'// asset\n' + b'x' * 3000
_BIG = b'A' * (5 * 1024 * 1024)          # > _CACHE_MAX_BYTES_PER_FILE (4 MiB)


@pytest.fixture
def static_root(tmp_path):
    (tmp_path / 'small.js').write_bytes(_SMALL)
    (tmp_path / 'big.js').write_bytes(_BIG)
    return tmp_path


def _conn(path: str, *, extensions=None, headers=None):
    from blackbull.connection import Connection
    return Connection.from_scope({
        'type': 'http',
        'method': 'GET',
        'path': path,
        'raw_path': path.encode(),
        'headers': headers or [],
        'query_string': b'',
        'extensions': extensions if extensions is not None
        else {'http.response.pathsend': {}},
    })


async def _drive(static_root, path, *, extensions=None, headers=None):
    """Run StaticFiles for *path*; return every object it sent."""
    mw = StaticFiles(url_prefix='/static', directory=str(static_root) + os.sep)
    sent: list = []

    async def send(event):
        sent.append(event)

    async def call_next(conn, receive, send):
        raise AssertionError('StaticFiles should have served this itself')

    await mw(_conn(path, extensions=extensions, headers=headers),
             None, send, call_next)
    return sent


@pytest.mark.asyncio
async def test_cache_hit_emits_one_native_response(static_root):
    sent = await _drive(static_root, '/static/small.js')

    assert sent, 'nothing was sent'
    assert all(isinstance(e, NativeResponse) for e in sent), (
        f'StaticFiles emitted a non-native event: '
        f'{[type(e).__name__ for e in sent]}')
    body = b''.join(e._body for e in sent if e._body is not None)
    assert body == _SMALL


@pytest.mark.asyncio
async def test_sendfile_form_is_native_and_carries_the_path(static_root):
    """The pathsend *function* survives; its ASGI dict shape does not."""
    sent = await _drive(static_root, '/static/big.js')

    assert all(isinstance(e, NativeResponse) for e in sent), (
        f'sendfile path emitted a dict: {[type(e).__name__ for e in sent]}')
    paths = [e.file_path for e in sent if e.file_path is not None]
    assert len(paths) == 1, f'expected one sendfile form, got {paths!r}'
    assert paths[0].endswith('big.js')


@pytest.mark.asyncio
async def test_streaming_fallback_is_native(static_root):
    """No pathsend extension (TLS / HTTP-2) → chunked, still native."""
    sent = await _drive(static_root, '/static/big.js', extensions={})

    assert all(isinstance(e, NativeResponse) for e in sent), (
        f'streaming fallback emitted a dict: '
        f'{[type(e).__name__ for e in sent]}')
    assert all(e.file_path is None for e in sent)
    body = b''.join(e._body for e in sent if e._body is not None)
    assert len(body) == len(_BIG)


@pytest.mark.asyncio
async def test_respond_path_is_native(static_root):
    """`_respond` — the 304 / 416 / 400 replies StaticFiles writes itself.

    (A *missing* file is not one of them: StaticFiles falls through to
    `call_next` so a route can still answer, so it never reaches `_respond`.)
    An unsatisfiable Range is the shortest deterministic trigger.
    """
    sent = await _drive(static_root, '/static/small.js',
                        headers=[(b'range', b'bytes=999999-')])

    assert all(isinstance(e, NativeResponse) for e in sent), (
        f'_respond emitted a dict: {[type(e).__name__ for e in sent]}')
    assert sent[0].status == 416


@pytest.mark.asyncio
async def test_range_request_is_native(static_root):
    sent = await _drive(static_root, '/static/small.js',
                        headers=[(b'range', b'bytes=0-9')])

    assert all(isinstance(e, NativeResponse) for e in sent), (
        f'range response emitted a dict: '
        f'{[type(e).__name__ for e in sent]}')
    assert sent[0].status == 206
    body = b''.join(e._body for e in sent if e._body is not None)
    assert body == _SMALL[:10]


# --- end-to-end: the wire is unchanged by the representation change --------

async def _get(port, path, accept_encoding=None, extra=b''):
    lines = [f'GET {path} HTTP/1.1'.encode(), b'Host: localhost']
    if accept_encoding:
        lines.append(b'Accept-Encoding: ' + accept_encoding)
    if extra:
        lines.append(extra)
    lines += [b'Connection: close', b'', b'']
    r, w = await asyncio.open_connection('127.0.0.1', port)
    try:
        w.write(b'\r\n'.join(lines))
        await w.drain()
        raw = await asyncio.wait_for(r.read(), 15.0)
    finally:
        w.close()
    head, _, body = raw.partition(b'\r\n\r\n')
    return head.split(b'\r\n')[0], head, body


@pytest.mark.asyncio
@pytest.mark.parametrize('name,expected_len', [('small.js', len(_SMALL)),
                                               ('big.js', len(_BIG))])
async def test_wire_bytes_unchanged(static_root, name, expected_len):
    app = BlackBull()
    app.static('/static', str(static_root) + os.sep)

    async with NativeTestServer(app) as server:
        status, _head, body = await _get(server.port, f'/static/{name}')

    assert status.startswith(b'HTTP/1.1 200'), status
    assert len(body) == expected_len


@pytest.mark.asyncio
async def test_sendfile_survives_the_compression_middleware(static_root):
    """The >4 MiB + Accept-Encoding case that used to return zero bytes."""
    from blackbull.middleware.compression import Compression

    app = BlackBull()
    app.use(Compression())
    app.static('/static', str(static_root) + os.sep)

    async with NativeTestServer(app) as server:
        status, _head, body = await _get(server.port, '/static/big.js',
                                         accept_encoding=b'gzip, br')

    assert status.startswith(b'HTTP/1.1 200'), status
    assert len(body) == len(_BIG)
