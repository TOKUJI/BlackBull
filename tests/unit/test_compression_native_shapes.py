"""Compression's native lane must cover every shape a response arrives in.

The native complete-response path exists so a response is decided, compressed,
and emitted as **one** ``NativeResponse`` — no ``to_asgi()`` expansion back
into dicts for the layer below to re-convert.  It was written against the
one-object shape a handler returning a ``Response`` produces, and silently did
not apply to the *split* shape ``StaticFiles`` produces (``http.response.start``
then ``http.response.body``, which become two half-populated objects).  The
HttpArena ``static`` lane pays that round trip on every request.

So these tests assert the predicate *fires* — by counting ``to_asgi()`` calls —
not merely that the bytes come out right.  A correctness-only test passes on
the slow path, which is exactly how the gap survived.

The pathsend case is a correctness bug rather than a performance one: a file
above the ``StaticFiles`` cache threshold, requested with ``Accept-Encoding``,
produced no response at all.
"""
import asyncio
import os
import pathlib

import pytest

from blackbull import BlackBull, JSONResponse
from blackbull.middleware.compression import Compression
from blackbull.testing import NativeTestServer
import blackbull.native as _native


@pytest.fixture
def count_to_asgi(monkeypatch):
    """Count ``NativeResponse.to_asgi()`` calls — the round trip's fingerprint."""
    calls = {'n': 0}
    original = _native.NativeResponse.to_asgi

    def counting(self):
        calls['n'] += 1
        return original(self)

    monkeypatch.setattr(_native.NativeResponse, 'to_asgi', counting)
    return calls


@pytest.fixture
def static_root(tmp_path):
    (tmp_path / 'small.js').write_bytes(b'// asset\n' + b'x' * 3000)
    # Above StaticFiles._CACHE_MAX_BYTES_PER_FILE (4 MiB) → pathsend lane.
    (tmp_path / 'big.js').write_bytes(b'A' * (5 * 1024 * 1024))
    return tmp_path


def _make_app(static_root) -> BlackBull:
    app = BlackBull()
    app.use(Compression())
    app.static('/static', str(static_root) + os.sep)

    @app.route(path='/json/{count:int}')
    async def json_endpoint(count: int):
        return JSONResponse({'items': [{'i': i} for i in range(count)]})

    return app


async def _get(port: int, path: str, accept_encoding: bytes | None = None,
               timeout: float = 15.0) -> tuple[bytes, dict[bytes, bytes], bytes]:
    """One request on its own connection; returns (status_line, headers, body)."""
    lines = [b'GET ' + path.encode() + b' HTTP/1.1', b'Host: localhost']
    if accept_encoding is not None:
        lines.append(b'Accept-Encoding: ' + accept_encoding)
    lines += [b'Connection: close', b'', b'']

    reader, writer = await asyncio.open_connection('127.0.0.1', port)
    try:
        writer.write(b'\r\n'.join(lines))
        await writer.drain()
        raw = await asyncio.wait_for(reader.read(), timeout)
    finally:
        writer.close()

    head, _, body = raw.partition(b'\r\n\r\n')
    rows = head.split(b'\r\n')
    headers: dict[bytes, bytes] = {}
    for row in rows[1:]:
        if b':' in row:
            name, _, value = row.partition(b':')
            headers[name.strip().lower()] = value.strip()
    return rows[0], headers, body


@pytest.mark.asyncio
async def test_split_static_response_is_not_round_tripped(
        static_root, count_to_asgi):
    """The static cache-hit shape must take the one-object native path.

    ``StaticFiles`` sends start and body separately, so the response reaches
    compression as a header-only object followed by a body-only object.  Both
    used to expand back into dicts and be re-converted below — the whole cost
    the native seam removed, paid twice.
    """
    async with NativeTestServer(_make_app(static_root)) as server:
        count_to_asgi['n'] = 0
        status, headers, body = await _get(
            server.port, '/static/small.js', b'gzip, br')

    assert status.startswith(b'HTTP/1.1 200'), status
    assert headers.get(b'content-encoding') in (b'br', b'gzip', b'zstd')
    assert body, 'compressed response had no body'
    assert count_to_asgi['n'] == 0, (
        f'the split static response expanded through to_asgi() '
        f'{count_to_asgi["n"]}x — the native fast path did not fire')


@pytest.mark.asyncio
async def test_complete_response_is_not_round_tripped(
        static_root, count_to_asgi):
    """Regression guard for the shape the fast path already covered."""
    async with NativeTestServer(_make_app(static_root)) as server:
        count_to_asgi['n'] = 0
        status, headers, body = await _get(server.port, '/json/50', b'gzip, br')

    assert status.startswith(b'HTTP/1.1 200'), status
    assert headers.get(b'content-encoding') in (b'br', b'gzip', b'zstd')
    assert count_to_asgi['n'] == 0


@pytest.mark.asyncio
async def test_large_static_file_with_accept_encoding_is_served(static_root):
    """A file on the pathsend lane must still be delivered, in full.

    Above the cache threshold ``StaticFiles`` hands the sender a path rather
    than bytes.  Compression cannot compress what it never sees, so it has to
    release the buffered header *before* the pathsend — the sender drops a
    pathsend it has no headers for, which produced an empty response.
    """
    expected = (static_root / 'big.js').stat().st_size

    async with NativeTestServer(_make_app(static_root)) as server:
        status, headers, body = await _get(
            server.port, '/static/big.js', b'gzip, br')

    assert status.startswith(b'HTTP/1.1 200'), (
        f'large static file with Accept-Encoding returned {status!r}')
    assert len(body) == expected, (
        f'expected {expected:,} body bytes, got {len(body):,}')
    assert body == b'A' * expected


@pytest.mark.asyncio
async def test_large_static_file_without_accept_encoding_is_unchanged(
        static_root):
    """The no-codec lane was never broken; keep it that way."""
    expected = (static_root / 'big.js').stat().st_size

    async with NativeTestServer(_make_app(static_root)) as server:
        status, headers, body = await _get(server.port, '/static/big.js')

    assert status.startswith(b'HTTP/1.1 200'), status
    assert len(body) == expected


@pytest.mark.asyncio
async def test_small_static_file_without_accept_encoding_is_uncompressed(
        static_root):
    """No negotiated codec → body forwarded verbatim, still cache-keyed."""
    async with NativeTestServer(_make_app(static_root)) as server:
        status, headers, body = await _get(server.port, '/static/small.js')

    assert status.startswith(b'HTTP/1.1 200'), status
    assert b'content-encoding' not in headers
    assert body == b'// asset\n' + b'x' * 3000
    # RFC 9110 §12.5.5 — a compressible, unencoded body must advertise that it
    # varies, or a shared cache replays this identity copy to a peer that
    # would have accepted an encoding.
    assert b'accept-encoding' in headers.get(b'vary', b'').lower()


@pytest.mark.asyncio
async def test_split_response_body_survives_a_round_trip(static_root):
    """The compressed bytes must actually decode back to the original."""
    import gzip
    import zlib

    async with NativeTestServer(_make_app(static_root)) as server:
        status, headers, body = await _get(
            server.port, '/static/small.js', b'gzip')

    assert status.startswith(b'HTTP/1.1 200'), status
    assert headers.get(b'content-encoding') == b'gzip'
    try:
        decoded = gzip.decompress(body)
    except (OSError, zlib.error) as exc:      # pragma: no cover - failure path
        pytest.fail(f'gzip body did not decompress: {exc}')
    assert decoded == b'// asset\n' + b'x' * 3000
    assert int(headers[b'content-length']) == len(body), (
        'Content-Length must describe the encoded body')
