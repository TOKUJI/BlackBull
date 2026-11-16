import asyncio
import gzip
import inspect
import threading

import pytest

from blackbull.connection import Connection
from blackbull.env import Settings, reset_settings_cache
from blackbull.middleware.compression import Compression
from blackbull.native import NativeResponse


BODY = b'compression settings ' * 30


async def reply(middleware, codec=b'gzip'):
    events = []
    conn = Connection.from_scope({'type': 'http', 'method': 'GET', 'path': '/',
                                 'headers': [(b'accept-encoding', codec)]})

    async def send(event):
        events.append(event)

    async def call_next(conn, receive, send):
        await send(NativeResponse.complete(200, [(b'content-type', b'text/plain')], BODY))

    await middleware(conn, None, send, call_next)
    [response] = events
    return response


def test_constructor_keeps_numeric_public_defaults():
    parameters = inspect.signature(Compression).parameters
    for name, default in [('min_size', Settings.compression_min_size),
                          ('executor_threshold', Settings.compression_executor_threshold),
                          ('executor_max_inflight', Settings.compression_max_inflight),
                          ('brotli_quality', Settings.brotli_quality)]:
        assert parameters[name].annotation is int
        assert isinstance(parameters[name].default, int)
        assert parameters[name].default == default
        assert repr(parameters[name].default) == repr(default)


@pytest.mark.asyncio
@pytest.mark.parametrize('min_size,compressed', [(None, False), (0, True), (100, True)])
async def test_optional_overrides_omit_inherited_values(monkeypatch, min_size, compressed):
    monkeypatch.setenv('BB_COMPRESSION_MIN_SIZE', '1000')
    overrides = {'min_size': min_size, 'executor_threshold': None,
                 'executor_max_inflight': None, 'brotli_quality': None}
    kwargs = {name: value for name, value in overrides.items() if value is not None}
    response = await reply(Compression(**kwargs))
    assert (response.header.get(b'content-encoding') == b'gzip') is compressed
    assert (gzip.decompress(response.body) if compressed else response.body) == BODY


@pytest.mark.asyncio
@pytest.mark.parametrize('explicit', [0, Settings.compression_min_size])
async def test_min_size_defaults_to_settings(monkeypatch, explicit):
    monkeypatch.setenv('BB_COMPRESSION_MIN_SIZE', '1000')
    response = await reply(Compression())
    assert b'content-encoding' not in response.header
    assert response.body == BODY
    overridden = await reply(Compression(min_size=explicit))
    assert overridden.header.get(b'content-encoding') == b'gzip'
    assert gzip.decompress(overridden.body) == BODY


@pytest.mark.asyncio
@pytest.mark.parametrize('threshold,explicit,offloaded', [
    ('1', None, True), ('0', None, False), ('1', 0, False), ('0', 1, True),
    ('1', Settings.compression_executor_threshold, False),
])
async def test_executor_threshold_defaults_to_settings(monkeypatch, threshold, explicit, offloaded):
    monkeypatch.setenv('BB_COMPRESSION_EXECUTOR_THRESHOLD', threshold)
    compress = gzip.compress
    threads = []

    def recorded(body):
        threads.append(threading.get_ident())
        return compress(body)

    monkeypatch.setattr(gzip, 'compress', recorded)
    kwargs = {} if explicit is None else {'executor_threshold': explicit}
    response = await reply(Compression(**kwargs))
    assert gzip.decompress(response.body) == BODY
    [thread] = threads
    assert (thread != threading.get_ident()) is offloaded


@pytest.mark.asyncio
@pytest.mark.parametrize('configured,explicit,capped', [
    ('1', None, True), ('0', None, False), ('1', 0, False), ('0', 1, True),
    ('1', Settings.compression_max_inflight, False),
])
async def test_inflight_limit_defaults_to_settings(monkeypatch, configured, explicit, capped):
    monkeypatch.setenv('BB_COMPRESSION_MAX_INFLIGHT', configured)
    monkeypatch.setenv('BB_COMPRESSION_EXECUTOR_THRESHOLD', '1')
    compress = gzip.compress
    entered, entered_twice, release = (threading.Event() for _ in range(3))
    calls = []

    def blocked(body):
        calls.append(threading.get_ident())
        entered.set()
        if len(calls) == 2:
            entered_twice.set()
        assert release.wait(5)
        return compress(body)

    monkeypatch.setattr(gzip, 'compress', blocked)
    kwargs = {} if explicit is None else {'executor_max_inflight': explicit}
    middleware = Compression(**kwargs)
    first = asyncio.create_task(reply(middleware))
    second = None
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        second = asyncio.create_task(reply(middleware))
        if capped:
            response = await asyncio.wait_for(asyncio.shield(second), 2)
            assert b'content-encoding' not in response.header
            assert response.body == BODY
        else:
            assert await asyncio.to_thread(entered_twice.wait, 2)
    finally:
        release.set()
        responses = await asyncio.gather(first, *([second] if second else []))
    assert responses[0].header.get(b'content-encoding') == b'gzip'
    if not capped:
        assert responses[1].header.get(b'content-encoding') == b'gzip'


@pytest.mark.asyncio
@pytest.mark.parametrize('quality,explicit', [('1', None), ('11', 0), ('99', None),
                                           ('1', Settings.brotli_quality)])
async def test_brotli_quality_defaults_to_validated_settings(monkeypatch, quality, explicit):
    brotli = pytest.importorskip('brotli')
    monkeypatch.setenv('BB_BROTLI_QUALITY', quality)
    kwargs = {} if explicit is None else {'brotli_quality': explicit}
    response = await reply(Compression(**kwargs), b'br')
    expected = explicit if explicit is not None else (4 if quality == '99' else int(quality))
    assert response.header.get(b'content-encoding') == b'br'
    assert response.body == brotli.compress(BODY, quality=expected)


@pytest.mark.parametrize('quality', [-1, 12])
def test_explicit_invalid_brotli_quality_is_rejected_at_construction(quality):
    with pytest.raises(ValueError, match='brotli_quality'):
        Compression(brotli_quality=quality)


@pytest.mark.asyncio
async def test_deprecated_alias_uses_the_same_current_settings(monkeypatch):
    from blackbull import middleware

    monkeypatch.setenv('BB_COMPRESSION_MIN_SIZE', '1000')
    with pytest.warns(DeprecationWarning):
        legacy = middleware.compress
    assert (await reply(legacy)).body == BODY
    monkeypatch.setenv('BB_COMPRESSION_MIN_SIZE', '1')
    reset_settings_cache()
    with pytest.warns(DeprecationWarning):
        legacy = middleware.compress
    assert gzip.decompress((await reply(legacy)).body) == BODY
