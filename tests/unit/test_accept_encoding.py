"""Response-level negotiation parity for dynamic and precompressed bodies."""
import gzip

import pytest

from blackbull.connection import Connection
from blackbull.headers import Headers
from blackbull.middleware.compression import Compression
from blackbull.middleware.static import StaticFiles
from blackbull.native import NativeResponse


BODY = b'negotiated response body ' * 32
CASES = [
    ([], b''),
    ([b''], b''),
    ([b'gzip'], b'gzip'),
    ([b'GZip;Q=1.000'], b'gzip'),
    ([b'gzip;q=0.001'], b'gzip'),
    ([b'gzip;q=0, identity;q=1'], b''),
    ([b'*;q=1,gzip;q=0'], b''),
    ([b'*;q=0,gzip;q=1'], b'gzip'),
    ([b'*;q=0.5'], b'gzip'),
    ([b'gzip;q=1', b'gzip;q=0'], b''),
    ([b'gzip;q=0', b'gzip;q=1'], b''),
    ([b'identity'], b''),
    ([b'gzip;q=NaN'], b''),
    ([b'gzip;q=inf'], b''),
    ([b'gzip;q=-1'], b''),
    ([b'gzip;q=1.1'], b''),
    ([b'gzip;q=.5'], b''),
    ([b'gzip;q=0.0001'], b''),
    ([b'gzip;q=bogus,*'], b''),
    ([b'gzip;q=0;q=1,*'], b''),
    ([b'gzip;unknown=1,*'], b''),
    ([b'gz\xffip'], b''),
    ([b'\tgzip ; q=0.5\t'], b'gzip'),
    ([b',,gzip,,'], b'gzip'),
    ([b'gzip;q=0.'], b''),
    ([b'gzip;q=1.'], b'gzip'),
    ([b'gzip;q=0.000'], b''),
    ([b'GZIP;Q=0'], b''),
    ([b'identity;q=0,gzip;q=0'], b''),
    ([b'identity;q=1,gzip;q=0.1'], b'gzip'),
    ([b'gzip;q=0.2,gzip;q=1'], b'gzip'),
    ([b'gzip;q=1e0'], b''),
    ([b'gzip;q="1"'], b''),
    ([b'gzip;q='], b''),
    ([b'gzip;q =1'], b''),
    ([b'gzip;q= 1'], b''),
    ([b'gzip;q=1\n'], b''),
    ([b'gzip;q=\x0b1'], b''),
]


async def _receive():
    return {'type': 'http.disconnect'}


async def _response(middleware, fields, *, dynamic):
    conn = Connection(method='GET', path='/body.txt', raw_path=b'/body.txt',
                      headers=Headers([(b'Accept-Encoding', v) for v in fields]),
                      type='http')
    events = []

    async def send(event):
        events.extend(event.to_asgi() if isinstance(event, NativeResponse) else [event])

    async def handler(conn, receive, send):
        await send(NativeResponse(status=200,
                                  header=[(b'content-type', b'text/plain')],
                                  body=BODY))

    if dynamic:
        await middleware(conn, _receive, send, handler)
    else:
        await middleware(conn, _receive, send)
    start = next(event for event in events if event['type'] == 'http.response.start')
    payload = b''.join(event.get('body', b'') for event in events
                       if event['type'] == 'http.response.body')
    return start['status'], Headers(start['headers']), payload


@pytest.mark.asyncio
@pytest.mark.parametrize(('fields', 'encoding'), CASES)
@pytest.mark.parametrize('path', ['dynamic', 'static', 'static-cache'])
async def test_acceptance_on_cache_miss_and_hit(tmp_path, fields, encoding, path):
    if path == 'dynamic':
        middleware = Compression(min_size=1)
        middleware._available = {'gzip': gzip.compress}
    else:
        (tmp_path / 'body.txt').write_bytes(BODY)
        (tmp_path / 'body.txt.gz').write_bytes(gzip.compress(BODY))
        middleware = StaticFiles(str(tmp_path), cache=path == 'static-cache')
    for _ in range(2):
        status, headers, payload = await _response(
            middleware, fields, dynamic=path == 'dynamic')
        assert status == 200
        assert headers.get(b'content-encoding') == encoding
        assert (gzip.decompress(payload) if encoding else payload) == BODY
        if path == 'dynamic' or encoding:
            assert b'accept-encoding' in headers.get(b'vary').lower()


@pytest.mark.asyncio
@pytest.mark.parametrize('path', ['dynamic', 'static'])
@pytest.mark.parametrize(('codecs', 'fields', 'expected'), [
    (('gzip',), [b'*'], 'gzip'),
    (('zstd', 'gzip'), [b'*'], 'zstd'),
    (('br', 'zstd', 'gzip'), [b'*'], 'br'),
    (('gzip',), [b'br;q=0.1,zstd;q=0.5,gzip;q=1'], 'gzip'),
    (('zstd', 'gzip'), [b'br;q=0.1,zstd;q=0.5,gzip;q=1'], 'zstd'),
    (('br', 'zstd', 'gzip'), [b'br;q=0.1,zstd;q=0.5,gzip;q=1'], 'br'),
    (('gzip',), [b'*', b'br;q=0'], 'gzip'),
    (('zstd', 'gzip'), [b'*', b'br;q=0'], 'zstd'),
    (('br', 'zstd', 'gzip'), [b'*', b'br;q=0'], 'zstd'),
    (('gzip',), [b'br;q=0,zstd;q=0,*'], 'gzip'),
    (('zstd', 'gzip'), [b'br;q=0,zstd;q=0,*'], 'gzip'),
    (('br', 'zstd', 'gzip'), [b'br;q=0,zstd;q=0,*'], 'gzip'),
    (('gzip',), [b'br;q=0', b'zstd;q=0', b'gzip;q=0', b'*'], ''),
    (('zstd', 'gzip'), [b'br;q=0', b'zstd;q=0', b'gzip;q=0', b'*'], ''),
    (('br', 'zstd', 'gzip'), [b'br;q=0', b'zstd;q=0', b'gzip;q=0', b'*'], ''),
])
async def test_available_codec_preference_and_refusals(tmp_path, path, codecs, fields, expected):
    compressors = {'gzip': gzip.compress}
    decompressors = {'gzip': gzip.decompress}
    if 'br' in codecs:
        brotli = pytest.importorskip('brotli')
        compressors['br'] = brotli.compress
        decompressors['br'] = brotli.decompress
    if 'zstd' in codecs:
        zstandard = pytest.importorskip('zstandard')
        compressors['zstd'] = zstandard.ZstdCompressor().compress
        decompressors['zstd'] = zstandard.ZstdDecompressor().decompress
    if path == 'dynamic':
        middleware = Compression(min_size=1)
        middleware._available = {name: compressors[name] for name in codecs}
    else:
        (tmp_path / 'body.txt').write_bytes(BODY)
        suffixes = {'br': '.br', 'zstd': '.zst', 'gzip': '.gz'}
        for name in codecs:
            (tmp_path / ('body.txt' + suffixes[name])).write_bytes(compressors[name](BODY))
        middleware = StaticFiles(str(tmp_path), cache=True)
    for _ in range(2):
        status, headers, payload = await _response(
            middleware, fields, dynamic=path == 'dynamic')
        assert status == 200
        assert headers.get(b'content-encoding') == expected.encode()
        assert (decompressors[expected](payload) if expected else payload) == BODY


@pytest.mark.asyncio
@pytest.mark.parametrize('path', ['dynamic', 'static'])
async def test_cache_keeps_every_accept_encoding_field(tmp_path, path):
    if path == 'dynamic':
        middleware = Compression(min_size=1)
        middleware._available = {'gzip': gzip.compress}
    else:
        (tmp_path / 'body.txt').write_bytes(BODY)
        (tmp_path / 'body.txt.gz').write_bytes(gzip.compress(BODY))
        middleware = StaticFiles(str(tmp_path), cache=True)
    for fields, expected in [
        ([b'gzip'], b'gzip'),
        ([b'gzip', b'gzip;q=0'], b''),
        ([b'gzip'], b'gzip'),
        ([b'gzip;q=0', b'*'], b''),
        ([b'gzip', b'*'], b'gzip'),
        ([b'gzip;q=0', b'*'], b''),
    ]:
        status, headers, payload = await _response(
            middleware, fields, dynamic=path == 'dynamic')
        assert status == 200
        assert headers.get(b'content-encoding') == expected
        assert (gzip.decompress(payload) if expected else payload) == BODY


@pytest.mark.asyncio
async def test_an_interim_head_reaches_the_wire_before_the_final_one():
    """RFC 9113 §8.1 / RFC 9110 §15.2: an interim response is part of the
    response it precedes. It carries no content, so there is nothing to
    compress and no decision that needs a body — holding it waits for a body
    that cannot come, and the interim never reaches the wire. The response
    behind it must keep compressing."""
    middleware = Compression(min_size=1)
    middleware._available = {'gzip': gzip.compress}
    conn = Connection(method='GET', path='/body.txt', raw_path=b'/body.txt',
                      headers=Headers([(b'Accept-Encoding', b'gzip')]),
                      type='http')
    events = []

    async def send(event):
        events.extend(
            event.to_asgi() if isinstance(event, NativeResponse) else [event])

    async def handler(conn, receive, send):
        await send(NativeResponse(status=103,
                                  header=[(b'link', b'</s.css>; rel=preload')]))
        await send(NativeResponse(status=200,
                                  header=[(b'content-type', b'text/plain')],
                                  body=BODY))

    await middleware(conn, _receive, send, handler)

    starts = [e for e in events if e['type'] == 'http.response.start']
    assert [s['status'] for s in starts] == [103, 200], starts
    assert dict(starts[0]['headers']).get(b'link') == b'</s.css>; rel=preload'
    payload = b''.join(e.get('body', b'') for e in events
                       if e['type'] == 'http.response.body')
    assert gzip.decompress(payload) == BODY, 'the final response lost compression'


@pytest.mark.asyncio
async def test_a_middleware_emitting_asgi_dicts_still_gets_compressed():
    """Third-party ASGI middleware predates the native seam and hands
    ``http.response.start`` dicts down.  The contract says the middleware's
    inner send wrapper sees only the native representation, so this pins
    whether a dict reaching the middleware band is normalised before the
    next layer — and therefore whether the dict branch there is a live
    compat surface or a shape that can no longer occur."""
    middleware = Compression(min_size=1)
    middleware._available = {'gzip': gzip.compress}
    conn = Connection(method='GET', path='/body.txt', raw_path=b'/body.txt',
                      headers=Headers([(b'Accept-Encoding', b'gzip')]),
                      type='http')
    events = []

    async def send(event):
        if isinstance(event, NativeResponse):
            events.extend(event.to_asgi())
        else:
            events.append(event)

    async def asgi_middleware(conn, receive, send, call_next):
        """A third-party shape: emits plain ASGI dicts, not NativeResponse."""
        async def dict_send(event):
            await send(event)
        await call_next(conn, receive, dict_send)

    async def handler(conn, receive, send):
        await send(NativeResponse(status=200,
                                  header=[(b'content-type', b'text/plain')],
                                  body=BODY))

    await asgi_middleware(conn, _receive, send,
                          lambda c, r, s: middleware(c, r, s, handler))

    starts = [e for e in events if e['type'] == 'http.response.start']
    assert starts, events
    payload = b''.join(e.get('body', b'') for e in events
                       if e['type'] == 'http.response.body')
    assert gzip.decompress(payload) == BODY
    assert b'accept-encoding' in dict(starts[0]['headers']).get(
        b'vary', b'').lower(), starts
