"""Every static selection ends inside the configured root (BLA-334).

The target and the index candidate take the walk in ``__call__``; the
variant picked in their place must be held to it too before reaching the
cache, the open or the pathsend.  Inward symlinks keep serving: containment,
not symlink-freedom, is the contract.
"""
import gzip
import pathlib

import pytest

from blackbull.headers import Headers
from blackbull.middleware.static import StaticFiles

pytestmark = pytest.mark.asyncio

SENTINEL = b'OUTSIDE_TEST_SENTINEL'
SUFFIXES = [('br', '.br'), ('zstd', '.zst'), ('gzip', '.gz')]


def _scope(method: str = 'GET', path: str = '/',
           headers: dict[str, str] | None = None):
    from blackbull.connection import Connection
    raw = [(k.lower().encode(), v.encode())
           for k, v in (headers or {}).items()]
    return Connection(method=method, path=path, raw_path=path.encode(),
                      headers=Headers(raw), type='http')


async def _noop_receive():
    return {'type': 'http.disconnect'}


def _native_collecting_send(events):
    from blackbull.native import NativeResponse

    async def send(event):
        if isinstance(event, NativeResponse):
            events.extend(event.to_asgi())
        else:
            events.append(event)
    return send


async def _collect(app, scope) -> tuple[dict, bytes]:
    events = []
    await app(scope, _noop_receive, _native_collecting_send(events))
    start = next(e for e in events if e.get('type') == 'http.response.start')
    body = b''.join(e.get('body', b'')
                    for e in events if e.get('type') == 'http.response.body')
    return start, body


@pytest.fixture
def env(tmp_path: pathlib.Path):
    """A served root with one file and an outside-root sentinel target."""
    www = tmp_path / 'www'
    www.mkdir()
    (www / 'item').write_bytes(b'ORIGINAL')
    outside = tmp_path / 'outside'
    outside.mkdir()
    (outside / 'sentinel').write_bytes(SENTINEL)
    return www, outside


@pytest.mark.parametrize('encoding,suffix', SUFFIXES)
@pytest.mark.parametrize('cache', [False, True])
async def test_an_outward_sibling_symlink_is_refused(env, encoding, suffix,
                                                     cache):
    www, outside = env
    (www / ('item' + suffix)).symlink_to(outside / 'sentinel')
    app = StaticFiles(directory=str(www), cache=cache)
    start, body = await _collect(app, _scope(
        path='/item', headers={'accept-encoding': encoding}))
    assert start['status'] == 400
    assert SENTINEL not in body


@pytest.mark.parametrize('encoding,suffix', SUFFIXES)
async def test_an_inward_sibling_symlink_still_serves(env, encoding, suffix):
    www, outside = env
    payload = gzip.compress(b'INNER')
    (www / ('real' + suffix)).write_bytes(payload)
    (www / ('item' + suffix)).symlink_to(www / ('real' + suffix))
    app = StaticFiles(directory=str(www))
    start, body = await _collect(app, _scope(
        path='/item', headers={'accept-encoding': encoding}))
    assert start['status'] == 200
    assert body == payload


async def test_the_original_file_serves_unchanged(env):
    www, _ = env
    app = StaticFiles(directory=str(www))
    start, body = await _collect(app, _scope(path='/item'))
    assert start['status'] == 200
    assert body == b'ORIGINAL'


async def test_the_index_sibling_takes_the_same_walk(env):
    www, outside = env
    (www / 'dir').mkdir()
    (www / 'dir' / 'index.html').write_bytes(b'INDEX')
    (www / 'dir' / 'index.html.gz').symlink_to(outside / 'sentinel')
    app = StaticFiles(directory=str(www), index='index.html')
    start, body = await _collect(app, _scope(
        path='/dir/', headers={'accept-encoding': 'gzip'}))
    assert start['status'] == 400
    assert SENTINEL not in body
    start, body = await _collect(app, _scope(path='/dir/'))
    assert (start['status'], body) == (200, b'INDEX')


@pytest.mark.parametrize('cache', [False, True])
async def test_a_sibling_swapped_between_requests_is_caught(env, cache):
    www, outside = env
    (www / 'item.gz').write_bytes(gzip.compress(b'GOOD'))
    app = StaticFiles(directory=str(www), cache=cache)
    start, body = await _collect(app, _scope(
        path='/item', headers={'accept-encoding': 'gzip'}))
    assert (start['status'], body) == (200, gzip.compress(b'GOOD'))
    (www / 'item.gz').unlink()
    (www / 'item.gz').symlink_to(outside / 'sentinel')
    start, body = await _collect(app, _scope(
        path='/item', headers={'accept-encoding': 'gzip'}))
    assert start['status'] == 400
    assert SENTINEL not in body and gzip.compress(b'GOOD') not in body


async def test_the_streaming_arm_is_bounded_too(env):
    www, outside = env
    (www / 'big').write_bytes(b'X' * 100)
    (www / 'big.gz').symlink_to(outside / 'sentinel')
    app = StaticFiles(directory=str(www))
    app._CACHE_MAX_BYTES_PER_FILE = 8
    start, body = await _collect(app, _scope(
        path='/big', headers={'accept-encoding': 'gzip'}))
    assert start['status'] == 400
    assert SENTINEL not in body


async def test_a_large_original_still_streams(env):
    www, _ = env
    (www / 'big').write_bytes(b'X' * 100)
    app = StaticFiles(directory=str(www))
    app._CACHE_MAX_BYTES_PER_FILE = 8
    start, body = await _collect(app, _scope(path='/big'))
    assert (start['status'], body) == (200, b'X' * 100)


async def test_range_skips_the_sibling_and_stays_inside(env):
    www, outside = env
    (www / 'item.gz').symlink_to(outside / 'sentinel')
    app = StaticFiles(directory=str(www))
    start, body = await _collect(app, _scope(
        path='/item', headers={'range': 'bytes=0-3',
                               'accept-encoding': 'gzip'}))
    assert start['status'] == 206
    assert body == b'ORIG'
    assert SENTINEL not in body


async def test_a_hardlinked_sibling_claims_its_encoding(env):
    """Variant identity is the filename alone (BLA-334 residual): a hard
    link of the original named ``item.gz`` is served as gzip although its
    bytes are plain.  ``st_nlink`` would not make identity robust; the
    tree's names must be honest."""
    www, _outside = env
    (www / 'item.gz').hardlink_to(www / 'item')
    start, body = await _collect(
        StaticFiles(directory=str(www)),
        _scope(path='/item', headers={'accept-encoding': 'gzip'}))
    assert start['status'] == 200
    headers = dict(start['headers'])
    assert headers.get(b'content-encoding') == b'gzip'
    assert body == b'ORIGINAL'


async def test_a_selected_variant_that_escapes_loses_the_whole_request(env):
    """Today's answer (BLA-334 residual): when the variant selection lands
    outside the root the request is refused with 400 even though the
    original would serve.  Fallback to the next candidate is not
    implemented; this pins what the code does today."""
    www, outside = env
    (www / 'item.gz').symlink_to(outside / 'sentinel')
    app = StaticFiles(directory=str(www))
    refused, _body = await _collect(
        app, _scope(path='/item', headers={'accept-encoding': 'gzip'}))
    assert refused['status'] == 400
    plain, body = await _collect(app, _scope(path='/item'))
    assert plain['status'] == 200
    assert body == b'ORIGINAL'


async def test_a_hardlink_to_an_outside_inode_serves_the_outside_bytes(env):
    """The sharper consequence of filename identity: a hard link to an
    outside inode passes the path-based boundary (the link lives inside
    the root) and the sentinel's bytes are served under the variant's
    claimed encoding.  Nothing here is measurable from the path."""
    www, outside = env
    (www / 'item.gz').hardlink_to(outside / 'sentinel')
    start, body = await _collect(
        StaticFiles(directory=str(www)),
        _scope(path='/item', headers={'accept-encoding': 'gzip'}))
    assert start['status'] == 200
    headers = dict(start['headers'])
    assert headers.get(b'content-encoding') == b'gzip'
    assert body == SENTINEL
